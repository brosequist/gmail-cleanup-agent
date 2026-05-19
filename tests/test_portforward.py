"""Tests for the optional PRE_RUN_COMMAND hook.

The hook lets the CLI launch a kubectl port-forward / SSH tunnel before
classify runs and tear it down on exit. Tests cover:
  - no-op when PRE_RUN_COMMAND unset
  - skip-spawn when the wait port is already accepting connections
  - timeout when the command runs but never opens the port
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

from gmail_cleanup import portforward


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Each test gets a clean module state and an isolated env."""
    monkeypatch.setattr(portforward, "_proc", None)
    # Ensure no leakage from the surrounding shell.
    for k in ("PRE_RUN_COMMAND", "PRE_RUN_WAIT_PORT", "PRE_RUN_WAIT_HOST",
              "PRE_RUN_WAIT_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)


def test_no_op_when_unset():
    # Should return cleanly without raising.
    portforward.maybe_start_pre_run()
    assert portforward._proc is None


def _free_port() -> int:
    """Bind to port 0, read what the OS picked, then release it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_skip_spawn_when_port_already_open(monkeypatch):
    """If PRE_RUN_WAIT_PORT is already accepting connections, the hook
    must NOT spawn anything — supports hand-started tunnels."""
    # Spin up a listener so the port-open check succeeds.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    spawned = []

    def _fake_popen(*args, **kwargs):
        spawned.append((args, kwargs))
        raise AssertionError("should not have been called")

    monkeypatch.setattr(portforward.subprocess, "Popen", _fake_popen)
    monkeypatch.setenv("PRE_RUN_COMMAND", "/bin/false")  # never actually invoked
    monkeypatch.setenv("PRE_RUN_WAIT_HOST", "127.0.0.1")
    monkeypatch.setenv("PRE_RUN_WAIT_PORT", str(port))

    try:
        portforward.maybe_start_pre_run()
    finally:
        listener.close()

    assert spawned == []
    assert portforward._proc is None


def test_timeout_when_port_never_opens(monkeypatch):
    """If the command runs but the wait port never opens, the hook must
    surface a TimeoutError (not hang forever)."""
    port = _free_port()
    monkeypatch.setenv(
        "PRE_RUN_COMMAND", "/bin/sh -c 'sleep 30'")
    monkeypatch.setenv("PRE_RUN_WAIT_HOST", "127.0.0.1")
    monkeypatch.setenv("PRE_RUN_WAIT_PORT", str(port))
    monkeypatch.setenv("PRE_RUN_WAIT_TIMEOUT", "1")  # fail fast

    with pytest.raises(TimeoutError):
        portforward.maybe_start_pre_run()

    # Hook should have cleaned up its own subprocess
    assert portforward._proc is None


def test_command_exits_early_surfaces_runtime_error(monkeypatch):
    """If PRE_RUN_COMMAND exits before the port opens, hook raises
    RuntimeError (with stderr) rather than hanging until timeout."""
    port = _free_port()
    monkeypatch.setenv("PRE_RUN_COMMAND", "/bin/sh -c 'exit 42'")
    monkeypatch.setenv("PRE_RUN_WAIT_HOST", "127.0.0.1")
    monkeypatch.setenv("PRE_RUN_WAIT_PORT", str(port))
    monkeypatch.setenv("PRE_RUN_WAIT_TIMEOUT", "5")

    with pytest.raises(RuntimeError):
        portforward.maybe_start_pre_run()


def test_terminate_proc_kills_running_subprocess(monkeypatch):
    """Manually exercise the atexit cleanup path: stash a real
    long-running subprocess, call _terminate_proc, verify it was
    terminated (no longer running)."""
    import subprocess

    proc = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    monkeypatch.setattr(portforward, "_proc", proc)

    portforward._terminate_proc()

    # The subprocess should be dead now
    assert proc.poll() is not None
    # Module state cleared
    assert portforward._proc is None


def test_terminate_proc_noop_when_no_process(monkeypatch):
    """_terminate_proc with no subprocess in flight should not raise."""
    monkeypatch.setattr(portforward, "_proc", None)
    portforward._terminate_proc()  # must not raise
    assert portforward._proc is None
