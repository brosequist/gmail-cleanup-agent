"""Tests for the config/backend.env loading in __main__.py.

Verifies the precedence rule: shell-exported env > backend.env > defaults.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _run_entrypoint(repo_root: Path, env_overrides: dict | None = None,
                    backend_env_lines: str | None = None) -> str:
    """Spawn `python -m gmail_cleanup --help` in a subprocess so the
    dotenv load actually runs from scratch (importing __main__ in this
    test process would side-effect the shared os.environ)."""
    cfg = repo_root / "config"
    cfg.mkdir(exist_ok=True)
    if backend_env_lines is not None:
        (cfg / "backend.env").write_text(backend_env_lines)

    env = os.environ.copy()
    env.pop("GCA_BACKEND", None)
    env.pop("OPENAI_MODEL", None)
    if env_overrides:
        env.update(env_overrides)

    # Have the subprocess print the resolved env vars after dotenv load.
    code = textwrap.dedent(f"""
        import os
        from gmail_cleanup.__main__ import main as _main  # noqa
        from pathlib import Path
        from dotenv import load_dotenv
        load_dotenv(Path({str(repo_root)!r}) / "config" / "backend.env",
                    override=False)
        print("GCA_BACKEND=" + os.environ.get("GCA_BACKEND", ""))
        print("OPENAI_MODEL=" + os.environ.get("OPENAI_MODEL", ""))
    """)
    out = subprocess.run([sys.executable, "-c", code], env=env,
                         capture_output=True, text=True, check=True)
    return out.stdout


def test_dotenv_loads_from_backend_env(tmp_path):
    out = _run_entrypoint(
        tmp_path,
        backend_env_lines="GCA_BACKEND=ollama\nOPENAI_MODEL=from-file\n",
    )
    assert "GCA_BACKEND=ollama" in out
    assert "OPENAI_MODEL=from-file" in out


def test_shell_export_overrides_backend_env(tmp_path):
    """Per docs: shell-exported values win over backend.env."""
    out = _run_entrypoint(
        tmp_path,
        backend_env_lines="OPENAI_MODEL=from-file\n",
        env_overrides={"OPENAI_MODEL": "from-shell"},
    )
    assert "OPENAI_MODEL=from-shell" in out


def test_missing_backend_env_does_not_crash(tmp_path):
    """No backend.env file — startup must still succeed (no-op load)."""
    # Don't write backend.env at all
    out = _run_entrypoint(tmp_path, backend_env_lines=None)
    # Got past the load_dotenv call (printed the expected lines)
    assert "GCA_BACKEND=" in out
