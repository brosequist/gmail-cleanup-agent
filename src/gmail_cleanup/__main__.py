"""Entry point for `python -m gmail_cleanup ...`.

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

from pathlib import Path

from dotenv import load_dotenv

# Repo root is two levels up from this file (src/gmail_cleanup/__main__.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_REPO_ROOT / "config" / "backend.env", override=False)

# Import AFTER load_dotenv so the backend factory in cli.py sees the
# loaded env vars when it inspects os.environ.
from .cli import main  # noqa: E402
from .portforward import maybe_start_pre_run  # noqa: E402

# Optional: if PRE_RUN_COMMAND is configured (e.g., a kubectl
# port-forward to reach an in-cluster Ollama / llama.cpp), launch it
# in the background and wait for the port to open before main() runs.
# Cleanup of the subprocess happens via an atexit hook.
maybe_start_pre_run()

main()
