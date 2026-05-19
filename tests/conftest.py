"""Shared pytest fixtures.

The reusable fake classes (FakeGmailClient, FakeBackend) live in
`tests/_fakes.py` so individual test modules can import them directly.
This file only defines fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gmail_cleanup.gmail_client import ThreadSummary


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Populated config directory with minimal labels.yaml, rules.md,
    whitelist.txt. Each test that uses this gets its own tmp copy."""
    d = tmp_path / "config"
    d.mkdir()
    (d / "labels.yaml").write_text(
        "existing:\n  - Filed\nauto_create:\n  Receipts: \"order confirmations\"\n"
        "  Family: \"personal\"\n"
    )
    (d / "rules.md").write_text("Trash all marketing email. Keep family.")
    (d / "whitelist.txt").write_text("@example-keep.com\n# comments ignored\n\n")
    # Stub OAuth artifacts so GmailClient instantiation never tries the
    # real flow. Tests monkeypatch the client; these files are just
    # placeholders for default-path validation.
    (d / "credentials.json").write_text("{}")
    (d / "token.json").write_text("{}")
    return d


@pytest.fixture
def patch_repo_root(monkeypatch, tmp_path, config_dir):
    """Force the CLI's working-dir + config-dir resolution to point at
    the tmp dir so default paths (state.json, dry-run.log) land in tmp,
    not in the real checkout.

    Uses the same env-var overrides that end users would use, plus a
    backstop monkeypatch on the module-level constants for any
    decoration-time defaults that snapshot them."""
    monkeypatch.setenv("GMAIL_CLEANUP_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("GMAIL_CLEANUP_CONFIG_DIR", str(config_dir))
    from gmail_cleanup import cli
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "CONFIG_DIR", config_dir)
    return tmp_path


@pytest.fixture
def fake_thread():
    """Helper to mint a ThreadSummary with sensible defaults."""
    def _make(tid="t1", sender="alice@acme.com", subject="hi",
              snippet="hello there", age_days=10,
              has_list_unsubscribe=False, body=""):
        return ThreadSummary(
            thread_id=tid, sender=sender, subject=subject, snippet=snippet,
            date="Mon, 1 Jan 2024", age_days=age_days,
            has_list_unsubscribe=has_list_unsubscribe, body=body,
        )
    return _make


@pytest.fixture
def decisions_json():
    """Helper to render a backend response wrapping a list of decisions."""
    def _make(items: list[dict]) -> str:
        return json.dumps({"decisions": items})
    return _make
