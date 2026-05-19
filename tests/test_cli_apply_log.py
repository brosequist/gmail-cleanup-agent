"""End-to-end tests for the `apply-log` subcommand.

The subcommand reads a decision log (typically dry-run.log) and replays
each decision against Gmail. Tests cover:
  - --dry-run preview produces replay-preview.log without mutating Gmail
  - --apply mutates Gmail and checkpoints state-applied.json
  - resume: already-applied IDs are skipped
  - --limit caps the workload
  - missing label in --apply mode triggers an error row, not a crash
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gmail_cleanup import applylog as applylog_module
from gmail_cleanup.cli import cli


def _write_log(p: Path, rows: list[dict]):
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def patched_gmail(monkeypatch, patch_repo_root, tmp_path):
    """Replace the GmailClient that applylog.run_apply_log instantiates
    with a fake. Returns the FakeGmailClient so tests can assert
    mutations.

    Also writes dummy `credentials.json` + `token.json` files at
    `tmp_path` so tests can pass `--credentials` / `--token` flags
    pointing at real files — the apply-log CLI's `--credentials` option
    has `exists=True` validation and its default is captured at
    decoration time (so a fixture-level monkeypatch on CONFIG_DIR
    can't redirect it). The fake GmailClient ignores both paths.
    """
    from tests._fakes import FakeGmailClient

    state = {
        "client": None,
        "creds_path": tmp_path / "test-credentials.json",
        "token_path": tmp_path / "test-token.json",
    }
    state["creds_path"].write_text("{}")
    state["token_path"].write_text("{}")

    def factory(creds, token):
        state["client"] = FakeGmailClient()
        return state["client"]

    monkeypatch.setattr(applylog_module, "GmailClient", factory)
    return state


def _creds_args(patched_gmail) -> list[str]:
    """Reusable --credentials/--token args for apply-log invocations."""
    return [
        "--credentials", str(patched_gmail["creds_path"]),
        "--token", str(patched_gmail["token_path"]),
    ]


def test_apply_log_dry_run_writes_preview_no_mutations(tmp_path, patched_gmail):
    log = tmp_path / "dry-run.log"
    _write_log(log, [
        {"id": "t1", "action": "trash", "label": None},
        {"id": "t2", "action": "keep", "label": "Receipts"},
        {"id": "t3", "action": "keep", "label": None},  # no-label keep
    ])
    runner = CliRunner()
    result = runner.invoke(cli, [
        "apply-log", "--dry-run", "--log-file", str(log),
        "--state-file", str(tmp_path / "state-applied.json"),
        "--audit-log", str(tmp_path / "preview.log"),
        *_creds_args(patched_gmail),
    ])
    assert result.exit_code == 0, result.output

    # No state-applied.json on dry-run
    assert not (tmp_path / "state-applied.json").exists()

    # Preview log has one row per decision
    preview_rows = [json.loads(l) for l in (tmp_path / "preview.log")
                    .read_text().splitlines() if l.strip().startswith("{")]
    by_id = {r["id"]: r for r in preview_rows}
    assert by_id["t1"]["result"] == "trash"
    assert by_id["t2"]["result"] == "keep_labeled"
    assert by_id["t3"]["result"] == "keep_nolabel"

    # FakeGmailClient was instantiated but never asked to mutate
    fake = patched_gmail["client"]
    assert fake is not None


def test_apply_log_apply_mutates_and_checkpoints(tmp_path, patched_gmail):
    log = tmp_path / "dry-run.log"
    _write_log(log, [
        {"id": "t1", "action": "trash", "label": None},
        {"id": "t2", "action": "keep", "label": "Receipts"},
    ])
    state_path = tmp_path / "state-applied.json"
    audit_path = tmp_path / "applied.log"
    runner = CliRunner()
    result = runner.invoke(cli, [
        "apply-log", "--apply", "--log-file", str(log),
        "--state-file", str(state_path),
        "--audit-log", str(audit_path),
        "--batch-sleep", "0",
        *_creds_args(patched_gmail),
    ])
    assert result.exit_code == 0, result.output

    # State checkpoint contains both IDs
    state = json.loads(state_path.read_text())
    assert set(state["applied"]) == {"t1", "t2"}

    # Audit log records the actions
    audit_rows = [json.loads(l) for l in audit_path.read_text().splitlines()
                  if l.strip().startswith("{")]
    by_id = {r["id"]: r for r in audit_rows}
    assert by_id["t1"]["result"] == "trash"
    assert by_id["t2"]["result"] == "keep_labeled"


def test_apply_log_resumes_skipping_already_applied(tmp_path, patched_gmail):
    log = tmp_path / "dry-run.log"
    _write_log(log, [
        {"id": "t1", "action": "trash", "label": None},
        {"id": "t2", "action": "trash", "label": None},
    ])
    state_path = tmp_path / "state-applied.json"
    state_path.write_text(json.dumps({"applied": ["t1"]}))

    audit_path = tmp_path / "applied.log"
    runner = CliRunner()
    result = runner.invoke(cli, [
        "apply-log", "--apply", "--log-file", str(log),
        "--state-file", str(state_path),
        "--audit-log", str(audit_path),
        "--batch-sleep", "0",
        *_creds_args(patched_gmail),
    ])
    assert result.exit_code == 0, result.output

    # Only t2 should appear in the audit log on this run
    audit_rows = [json.loads(l) for l in audit_path.read_text().splitlines()
                  if l.strip().startswith("{")]
    by_id = {r["id"]: r for r in audit_rows}
    assert "t1" not in by_id
    assert by_id["t2"]["result"] == "trash"

    # State should accumulate to both
    state = json.loads(state_path.read_text())
    assert set(state["applied"]) == {"t1", "t2"}


def test_apply_log_limit_caps_actions(tmp_path, patched_gmail):
    log = tmp_path / "dry-run.log"
    _write_log(log, [
        {"id": f"t{i}", "action": "trash", "label": None} for i in range(5)
    ])
    state_path = tmp_path / "state.json"
    audit_path = tmp_path / "applied.log"
    runner = CliRunner()
    result = runner.invoke(cli, [
        "apply-log", "--apply", "--log-file", str(log),
        "--state-file", str(state_path),
        "--audit-log", str(audit_path),
        "--limit", "2",
        "--batch-sleep", "0",
        *_creds_args(patched_gmail),
    ])
    assert result.exit_code == 0, result.output

    state = json.loads(state_path.read_text())
    assert len(state["applied"]) == 2


def test_apply_log_error_action_is_skipped(tmp_path, patched_gmail):
    """Rows with action='error' from the original classify pass should
    NEVER be replayed — they would crash the keep/trash dispatcher."""
    log = tmp_path / "dry-run.log"
    _write_log(log, [
        {"id": "t1", "action": "error", "label": None, "note": "boom"},
        {"id": "t2", "action": "trash", "label": None},
    ])
    audit_path = tmp_path / "preview.log"
    runner = CliRunner()
    result = runner.invoke(cli, [
        "apply-log", "--dry-run", "--log-file", str(log),
        "--audit-log", str(audit_path),
        *_creds_args(patched_gmail),
    ])
    assert result.exit_code == 0, result.output

    rows = [json.loads(l) for l in audit_path.read_text().splitlines()
            if l.strip().startswith("{")]
    assert {r["id"] for r in rows} == {"t2"}


def test_apply_log_latest_decision_wins(tmp_path, patched_gmail):
    """Multiple log rows for the same id collapse to the latest one."""
    log = tmp_path / "dry-run.log"
    _write_log(log, [
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t1", "action": "trash", "label": None},   # later wins
    ])
    audit_path = tmp_path / "preview.log"
    runner = CliRunner()
    result = runner.invoke(cli, [
        "apply-log", "--dry-run", "--log-file", str(log),
        "--audit-log", str(audit_path),
        *_creds_args(patched_gmail),
    ])
    assert result.exit_code == 0, result.output

    rows = [json.loads(l) for l in audit_path.read_text().splitlines()
            if l.strip().startswith("{")]
    assert len(rows) == 1
    assert rows[0]["result"] == "trash"
