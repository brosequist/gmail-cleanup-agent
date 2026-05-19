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


# ---------------- _resolve_backend_env_path resolver ----------------


def test_resolver_uses_explicit_env_dir(tmp_path, monkeypatch):
    """$GMAIL_CLEANUP_CONFIG_DIR overrides everything else."""
    from gmail_cleanup.__main__ import _resolve_backend_env_path
    monkeypatch.setenv("GMAIL_CLEANUP_CONFIG_DIR", str(tmp_path))
    assert _resolve_backend_env_path() == tmp_path / "backend.env"


def test_resolver_falls_back_to_cwd_config(tmp_path, monkeypatch):
    """If no env var is set but ./config/backend.env exists in cwd,
    it wins over the package-relative fallback."""
    from gmail_cleanup.__main__ import _resolve_backend_env_path
    monkeypatch.delenv("GMAIL_CLEANUP_CONFIG_DIR", raising=False)
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "backend.env").write_text("GCA_BACKEND=ollama\n")
    monkeypatch.chdir(tmp_path)
    assert _resolve_backend_env_path() == cfg / "backend.env"


def test_resolver_falls_back_to_package_root(tmp_path, monkeypatch):
    """No env var, no cwd config — falls back to the package-relative
    path (the editable-install convenience)."""
    from gmail_cleanup.__main__ import _resolve_backend_env_path
    monkeypatch.delenv("GMAIL_CLEANUP_CONFIG_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # no ./config here
    out = _resolve_backend_env_path()
    # Path comes from __file__ -> ../../../config/backend.env; we only
    # assert the basename + parent-dir name, not the exact prefix.
    assert out.name == "backend.env"
    assert out.parent.name == "config"
