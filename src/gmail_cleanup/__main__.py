"""Entry point for `python -m gmail_cleanup ...` and the
`gmail-cleanup` console script.

Before dispatching to the Click CLI, we load runtime configuration from
`config/backend.env` (if present). This is the preferred way to set the
backend env vars (GCA_BACKEND, OLLAMA_*, OPENAI_*, CLAUDE_*, ANTHROPIC_*)
without having to re-export them in every shell session.

Precedence (highest wins):
  1. Variables already exported in the current shell (or set inline:
     `OPENAI_MODEL=foo python -m gmail_cleanup ...`).
  2. Values in `config/backend.env`.
  3. Defaults baked into each backend.

Shell-exported values winning is intentional: it lets you keep a
known-good `config/backend.env` checked in (locally) while doing
one-off runs with overrides.

`config/backend.env.example` is the template; copy it to
`config/backend.env` and fill in the values for whichever backend you
want to use.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


def _resolve_backend_env_path() -> Path:
    """Where to look for the optional `backend.env` dotenv file.

    Order (highest precedence first):
      1. $GMAIL_CLEANUP_CONFIG_DIR/backend.env (explicit override; Docker uses this)
      2. ./config/backend.env in the current working directory
      3. <package-root>/config/backend.env — only meaningful for editable
         installs (`pip install -e .`); harmless on wheel installs since
         load_dotenv silently no-ops on missing files.
    """
    env = os.environ.get("GMAIL_CLEANUP_CONFIG_DIR")
    if env:
        return Path(env).expanduser() / "backend.env"
    cwd_path = Path.cwd() / "config" / "backend.env"
    if cwd_path.is_file():
        return cwd_path
    # Editable-install fallback: __file__ -> src/gmail_cleanup/__main__.py;
    # parents[2] is the repo root next to its config/ dir.
    return Path(__file__).resolve().parents[2] / "config" / "backend.env"


def main():
    """Console-script entry point used by both `python -m gmail_cleanup`
    and the installed `gmail-cleanup` command. Loads backend.env, runs
    the optional pre-run hook, then dispatches to the Click CLI."""
    load_dotenv(_resolve_backend_env_path(), override=False)

    # Import AFTER load_dotenv so the backend factory sees the loaded env
    # vars when it inspects os.environ.
    from .cli import main as cli_main
    from .portforward import maybe_start_pre_run

    # Optional: if PRE_RUN_COMMAND is configured (e.g., a kubectl
    # port-forward to reach an in-cluster Ollama / llama.cpp), launch it
    # in the background and wait for the port to open before main()
    # runs. Cleanup happens via an atexit hook.
    maybe_start_pre_run()

    cli_main()


if __name__ == "__main__":
    main()
