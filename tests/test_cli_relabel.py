"""End-to-end tests for the `relabel` subcommand.

The subcommand re-asks the LLM for the best label on already-kept
emails. It MUST NEVER trash anything. Tests cover:
  - dry-run produces relabel.log without mutating Gmail
  - apply moves the label in Gmail
  - trash / error rows in the source log are ignored
  - --refetch-snippets pulls the snippet from Gmail for richer context
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gmail_cleanup import cli as cli_module
from gmail_cleanup.cli import cli


def _write_log(p: Path, rows: list[dict]):
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def patched(monkeypatch, patch_repo_root):
    from tests._fakes import FakeBackend, FakeGmailClient

    state = {"backend_responses": [], "threads": []}

    def fake_client_factory():
        gc = FakeGmailClient(threads_to_yield=state["threads"])
        state["client"] = gc
        return gc

    def fake_get_backend():
        b = FakeBackend(responses=state["backend_responses"])
        state["backend"] = b
        return b

    monkeypatch.setattr(cli_module, "_client", fake_client_factory)
    monkeypatch.setattr(cli_module, "get_backend", fake_get_backend)
    return state


def test_relabel_dry_run_proposes_changes_without_mutating(tmp_path, patched):
    src = tmp_path / "src.log"
    _write_log(src, [
        {"id": "t1", "from": "a", "subject": "s",
         "action": "keep", "label": "Family"},
        {"id": "t2", "from": "b", "subject": "s",
         "action": "trash", "label": None},   # MUST be ignored
        {"id": "t3", "from": "c", "subject": "s",
         "action": "error", "label": None},   # MUST be ignored
    ])
    out_log = tmp_path / "relabel.log"
    patched["backend_responses"] = [json.dumps({"decisions": [
        {"id": "t1", "label": "Receipts"},
    ]})]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "relabel", "--dry-run", "--input-log", str(src),
        "--log-file", str(out_log),
        "--state-file", str(tmp_path / "relabel-state.json"),
        "--concurrency", "1",
    ])
    assert result.exit_code == 0, result.output

    rows = [json.loads(l) for l in out_log.read_text().splitlines()
            if l.strip().startswith("{")]
    by_id = {r["id"]: r for r in rows}
    assert by_id["t1"]["old_label"] == "Family"
    assert by_id["t1"]["new_label"] == "Receipts"
    assert by_id["t1"]["changed"] is True
    assert "t2" not in by_id and "t3" not in by_id

    # Dry-run never calls Gmail.
    assert patched["client"].modified == []


def test_relabel_apply_modifies_labels(tmp_path, patched):
    src = tmp_path / "src.log"
    _write_log(src, [
        {"id": "t1", "from": "a", "subject": "s",
         "action": "keep", "label": "Family"},
    ])
    patched["backend_responses"] = [json.dumps({"decisions": [
        {"id": "t1", "label": "Receipts"},
    ]})]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "relabel", "--apply", "--input-log", str(src),
        "--log-file", str(tmp_path / "relabel.log"),
        "--state-file", str(tmp_path / "relabel-state.json"),
        "--concurrency", "1",
        "--confirm-every", "0",
    ])
    assert result.exit_code == 0, result.output

    # Single modify_thread_labels call with add=[Receipts.id], remove=[Family.id]
    assert len(patched["client"].modified) == 1
    mod = patched["client"].modified[0]
    assert mod["id"] == "t1"
    assert "Label_1" in mod["add"]       # Receipts -> Label_1
    assert "Label_2" in mod["remove"]    # Family -> Label_2


def test_relabel_unchanged_label_no_mutation(tmp_path, patched):
    """If the LLM returns the same label, no Gmail call is made."""
    src = tmp_path / "src.log"
    _write_log(src, [
        {"id": "t1", "from": "a", "subject": "s",
         "action": "keep", "label": "Receipts"},
    ])
    patched["backend_responses"] = [json.dumps({"decisions": [
        {"id": "t1", "label": "Receipts"},  # unchanged
    ]})]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "relabel", "--apply", "--input-log", str(src),
        "--log-file", str(tmp_path / "relabel.log"),
        "--state-file", str(tmp_path / "relabel-state.json"),
        "--concurrency", "1",
        "--confirm-every", "0",
    ])
    assert result.exit_code == 0, result.output
    assert patched["client"].modified == []


def test_relabel_refetch_snippets_calls_fetch_thread_meta(tmp_path, patched,
                                                          fake_thread,
                                                          monkeypatch):
    src = tmp_path / "src.log"
    _write_log(src, [
        {"id": "t1", "from": "a", "subject": "s",
         "action": "keep", "label": "Family"},
    ])
    # Pre-populate the fake's threads so fetch_thread_meta(t1) succeeds.
    # The fixture creates the client lazily — set up a custom factory.
    from tests._fakes import FakeGmailClient
    custom = FakeGmailClient(threads_to_yield=[
        fake_thread(tid="t1", sender="real@sender.com", subject="real subject",
                    snippet="real snippet"),
    ])

    fetch_calls = []
    orig_fetch = custom.fetch_thread_meta

    def tracked(tid):
        fetch_calls.append(tid)
        return orig_fetch(tid)
    custom.fetch_thread_meta = tracked

    monkeypatch.setattr(cli_module, "_client", lambda: custom)

    patched["backend_responses"] = [json.dumps({"decisions": [
        {"id": "t1", "label": "Receipts"},
    ]})]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "relabel", "--dry-run", "--input-log", str(src),
        "--log-file", str(tmp_path / "relabel.log"),
        "--state-file", str(tmp_path / "relabel-state.json"),
        "--concurrency", "1",
        "--refetch-snippets",
    ])
    assert result.exit_code == 0, result.output
    # Verify the snippet refetch happened
    assert fetch_calls == ["t1"]


def test_relabel_backend_error_keeps_existing_label(tmp_path, patched):
    """If the backend raises on the first call, relabel must NOT drop
    any labels — the existing label survives. This is the safe-failure
    contract that makes relabel safe to re-run after a backend hiccup."""
    src = tmp_path / "src.log"
    _write_log(src, [
        {"id": "t1", "from": "a", "subject": "s",
         "action": "keep", "label": "Family"},
    ])

    class ExplodingBackend:
        def classify_batch(self, prompt):
            raise RuntimeError("relabel backend went down")

    # Replace the FakeBackend that the fixture created with an exploder
    patched["backend"] = ExplodingBackend()

    from gmail_cleanup import cli as cli_module
    import pytest as _pt
    # Re-monkey via the existing patched dict — get_backend was set
    # already; we need it to return ExplodingBackend now.
    cli_module.get_backend = lambda: patched["backend"]

    runner = CliRunner()
    result = runner.invoke(cli, [
        "relabel", "--apply", "--input-log", str(src),
        "--log-file", str(tmp_path / "relabel.log"),
        "--state-file", str(tmp_path / "relabel-state.json"),
        "--concurrency", "1",
        "--confirm-every", "0",
    ])
    assert result.exit_code == 0, result.output
    # No labels were moved despite --apply
    assert patched["client"].modified == []
    # The log shows the error row with the original label preserved
    rows = [json.loads(l) for l in (tmp_path / "relabel.log").read_text()
            .splitlines() if l.strip().startswith("{")]
    assert any("relabel backend went down" in r.get("note", "") for r in rows)
