"""Optional pre-run setup hook.

If `PRE_RUN_COMMAND` is set in the environment, the CLI launches it as
a background subprocess before classify starts, and tears it down on
exit (normal or Ctrl+C). Common use is `kubectl port-forward` to reach
an Ollama / llama.cpp / LM Studio backend running in a Kubernetes
cluster, but the hook is generic — any setup that exposes a local
port (SSH tunnels, socat proxies, custom service launchers) works.

Config (set in shell or `config/backend.env`):
  PRE_RUN_COMMAND       Command line to launch in background. The CLI
                        word-splits via shlex; quote arguments
                        normally. Required to activate; absent = no-op.

  PRE_RUN_WAIT_PORT     If set, the CLI waits until TCP <host>:<port>
                        accepts connections before proceeding. Strongly
                        recommended — without it, the classifier will
                        try to talk to the backend before the tunnel
                        is up and fail.

  PRE_RUN_WAIT_HOST     Host to probe. Default: localhost.

  PRE_RUN_WAIT_TIMEOUT  Max seconds to wait for the port to open.
                        Default: 30.

If `PRE_RUN_WAIT_PORT` is already open at startup (e.g., you have a
port-forward running by hand from a previous session), the CLI logs a
note and skips spawning anything — your hand-started tunnel keeps
ownership and stays running after the CLI exits.

Cleanup happens via an atexit hook. The subprocess is launched with
`start_new_session=True` so that a Ctrl+C in the terminal doesn't kill
it directly — our atexit hook runs in the parent and kills it cleanly
after the main work has wound down.
"""

from __future__ import annotations

import atexit
import logging
import os
import shlex
import socket
import subprocess
import time

logger = logging.getLogger(__name__)

# Module-level handle to the spawned subprocess so the atexit hook can
# find it. None when no pre-run is active.
_proc: subprocess.Popen | None = None


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _terminate_proc() -> None:
    """atexit hook — terminate the pre-run subprocess if still running."""
    global _proc
    if _proc is None:
        return
    if _proc.poll() is not None:
        _proc = None
        return
    logger.info("pre-run: stopping background subprocess (pid %d)", _proc.pid)
    try:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
            _proc.wait(timeout=2)
    except Exception as e:  # noqa: BLE001
        logger.warning("pre-run cleanup failed: %s", e)
    _proc = None


def maybe_start_pre_run() -> None:
    """Launch PRE_RUN_COMMAND if configured. No-op otherwise.

    Raises:
        RuntimeError: if the subprocess exited before the wait port
            opened (typically misconfigured command — pulls stderr for
            the error message).
        TimeoutError: if the wait port didn't open within
            PRE_RUN_WAIT_TIMEOUT seconds.
    """
    global _proc

    cmd = os.environ.get("PRE_RUN_COMMAND")
    if not cmd:
        return

    wait_port_raw = os.environ.get("PRE_RUN_WAIT_PORT")
    wait_host = os.environ.get("PRE_RUN_WAIT_HOST", "localhost")
    wait_timeout = int(os.environ.get("PRE_RUN_WAIT_TIMEOUT", "30"))

    # Already-open port → assume a previously-launched tunnel is alive
    # and skip the spawn. Lets users hand-start kubectl port-forward
    # separately when they want manual control over its lifecycle.
    if wait_port_raw:
        port = int(wait_port_raw)
        if _port_open(wait_host, port, timeout=1.0):
            logger.info(
                "pre-run: %s:%d already open — skipping PRE_RUN_COMMAND",
                wait_host, port,
            )
            return

    logger.info("pre-run: launching %s", cmd)
    args = shlex.split(cmd)
    _proc = subprocess.Popen(  # noqa: S603 — command comes from user config
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        # New session so Ctrl+C in the terminal doesn't deliver SIGINT
        # to the subprocess directly — atexit handles cleanup.
        start_new_session=True,
    )
    atexit.register(_terminate_proc)

    if not wait_port_raw:
        # No wait configured — caller assumes the command is fast to
        # come up, or they're managing readiness another way.
        logger.warning(
            "pre-run: PRE_RUN_COMMAND launched without PRE_RUN_WAIT_PORT; "
            "the classifier may fail if the tunnel isn't up yet"
        )
        return

    port = int(wait_port_raw)
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        if _port_open(wait_host, port, timeout=1.0):
            logger.info("pre-run: %s:%d is up", wait_host, port)
            return
        if _proc.poll() is not None:
            stderr = (_proc.stderr.read().decode(errors="replace")
                      if _proc.stderr else "")
            _proc = None
            raise RuntimeError(
                f"PRE_RUN_COMMAND exited (code {_proc.returncode if _proc else '?'}) "
                f"before {wait_host}:{port} opened. Last stderr:\n"
                f"{stderr.strip()[:500]}"
            )
        time.sleep(0.5)

    # Timed out — terminate and surface the timeout
    _terminate_proc()
    raise TimeoutError(
        f"PRE_RUN_COMMAND launched but {wait_host}:{port} did not "
        f"accept connections within {wait_timeout} seconds"
    )
