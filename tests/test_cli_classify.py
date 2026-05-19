"""End-to-end tests for the `classify` subcommand.

Exercises the Click CLI with mocked GmailClient + backend. Verifies:
  - dry-run produces decision log + state.json but never mutates Gmail
  - --apply triggers trash_thread / add_label_to_thread
  - --limit caps the number of processed threads
  - --include-body flows through to search_threads
  - --console-log mirrors stderr to a file
  - --retry-errors re-classifies threads previously marked "error"
  - whitelisted senders short-circuit the LLM entirely
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from gmail_cleanup import cli as cli_module
from gmail_cleanup.cli import cli


@pytest.fixture
def patched(monkeypatch, patch_repo_root, fake_thread, decisions_json):
    """Common monkeypatch — replaces GmailClient + get_backend with fakes.

    Returns a dict with handles so tests can inject canned threads + LLM
    responses before invoking the CLI.
    """
    from tests._fakes import FakeBackend, FakeGmailClient

    state = {
        "threads": [
            fake_thread(tid="t1", sender="alice@acme.com", subject="receipt"),
            fake_thread(tid="t2", sender="newsletter@spam.example",
                        subject="50% off", has_list_unsubscribe=True),
        ],
        "backend_responses": [],
    }

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


def _invoke_classify(tmp_path: Path, extra_args: list[str]) -> object:
    """Helper — always pass explicit state/log paths so tests don't
    inherit the real repo's state.json (Click captures the
    `REPO_ROOT / 'state.json'` default at decoration time, so the
    fixture's REPO_ROOT monkeypatch doesn't reach it)."""
    runner = CliRunner()
    base = [
        "classify",
        "--state-file", str(tmp_path / "state.json"),
        "--log-file", str(tmp_path / "dry-run.log"),
    ]
    return runner.invoke(cli, base + extra_args)


def _read_log(p: Path) -> list[dict]:
    rows: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("==="):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def test_classify_dry_run_does_not_mutate(tmp_path, patched, decisions_json):
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--dry-run", "--concurrency", "1", "--query", "anything",
    ])
    assert result.exit_code == 0, result.output

    log_rows = _read_log(tmp_path / "dry-run.log")
    by_id = {r["id"]: r for r in log_rows}
    assert by_id["t1"]["action"] == "keep"
    assert by_id["t1"]["label"] == "Receipts"
    assert by_id["t2"]["action"] == "trash"

    # Nothing mutated in Gmail
    assert patched["client"].trashed == []
    assert patched["client"].labeled == []

    # State checkpoint written
    state = json.loads((tmp_path / "state.json").read_text())
    assert set(state["processed"]) == {"t1", "t2"}


def test_classify_apply_trashes_and_labels(tmp_path, patched, decisions_json):
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--apply", "--concurrency", "1",
        "--confirm-every", "0",  # no interactive prompt
        "--query", "anything",
    ])
    assert result.exit_code == 0, result.output

    assert patched["client"].trashed == ["t2"]
    # t1 got the Receipts label (Label_1 in the fake)
    assert ("t1", "Label_1") in patched["client"].labeled


def test_classify_limit_caps_thread_count(tmp_path, patched, fake_thread,
                                          decisions_json):
    patched["threads"] = [fake_thread(tid=f"u{i}") for i in range(5)]
    patched["backend_responses"] = [decisions_json([
        {"id": f"u{i}", "action": "trash", "label": None} for i in range(2)
    ])]
    result = _invoke_classify(tmp_path, [
        "--dry-run", "--limit", "2", "--concurrency", "1", "--batch-size", "2",
    ])
    assert result.exit_code == 0, result.output

    log_rows = _read_log(tmp_path / "dry-run.log")
    assert {r["id"] for r in log_rows} == {"u0", "u1"}


def test_classify_include_body_plumbs_through(tmp_path, patched,
                                              decisions_json):
    """--include-body should set include_body=True on search_threads."""
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "trash", "label": None},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--dry-run", "--include-body", "--concurrency", "1",
    ])
    assert result.exit_code == 0, result.output
    assert patched["client"].last_include_body is True


def test_classify_no_include_body_by_default(tmp_path, patched, decisions_json):
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "trash", "label": None},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, ["--dry-run", "--concurrency", "1"])
    assert result.exit_code == 0, result.output
    assert patched["client"].last_include_body is False


def test_classify_console_log_attaches_file_handler(tmp_path, patched,
                                                    decisions_json):
    """--console-log PATH should add a FileHandler to the root logger
    that writes to PATH. Asserting the handler's existence is more
    reliable than asserting captured content under pytest's log
    capture — but we still check the file gets created."""
    import logging

    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "trash", "label": None},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    console_log = tmp_path / "console.log"
    # Clear any FileHandlers from previous tests in this process
    root = logging.getLogger()
    pre_fhs = {h for h in root.handlers if isinstance(h, logging.FileHandler)}

    result = _invoke_classify(tmp_path, [
        "--dry-run", "--concurrency", "1", "--console-log", str(console_log),
    ])
    assert result.exit_code == 0, result.output
    assert console_log.exists()

    new_fhs = [h for h in root.handlers
               if isinstance(h, logging.FileHandler) and h not in pre_fhs]
    target_paths = [h.baseFilename for h in new_fhs]
    assert str(console_log) in target_paths, (
        f"--console-log did not register a FileHandler for {console_log}; "
        f"saw {target_paths}")


def test_classify_whitelist_short_circuits_llm(tmp_path, patched, fake_thread,
                                               config_dir, decisions_json):
    # Whitelisted sender — backend should never be called for t1.
    patched["threads"] = [
        fake_thread(tid="t1", sender="trusted@example-keep.com"),
        fake_thread(tid="t2", sender="random@spam.example"),
    ]
    # Only t2 needs an LLM decision
    patched["backend_responses"] = [decisions_json([
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--dry-run", "--concurrency", "1", "--batch-size", "5",
    ])
    assert result.exit_code == 0, result.output

    rows = _read_log(tmp_path / "dry-run.log")
    by_id = {r["id"]: r for r in rows}
    assert by_id["t1"]["action"] == "keep"
    assert by_id["t1"]["note"] == "whitelist"
    # And t2 went through the LLM as normal
    assert by_id["t2"]["action"] == "trash"


def test_classify_retry_errors_reclassifies_only_errored(tmp_path, patched,
                                                        fake_thread,
                                                        decisions_json):
    """Seed a log with one keep + one error row, plus a matching
    state.json, then run with --retry-errors. The errored ID should be
    re-classified, the kept one skipped."""
    log = tmp_path / "dry-run.log"
    log.write_text(
        '=== prior run ===\n'
        + '{"id":"r1","from":"a","subject":"s","action":"keep","label":null,"note":""}\n'
        + '{"id":"r2","from":"b","subject":"s","action":"error","label":null,"note":"backend: boom"}\n'
    )
    (tmp_path / "state.json").write_text(
        json.dumps({"processed": ["r1", "r2"]}))

    patched["threads"] = [fake_thread(tid="r2", sender="x@y", subject="redo")]
    # Backend returns a fresh decision for r2
    patched["backend_responses"] = [decisions_json([
        {"id": "r2", "action": "keep", "label": "Family"},
    ])]
    result = _invoke_classify(tmp_path, [
        "--dry-run", "--concurrency", "1",
        "--retry-errors", "--batch-size", "5",
    ])
    assert result.exit_code == 0, result.output

    # New keep row appended for r2 after the original error
    rows = _read_log(log)
    last_r2 = [r for r in rows if r["id"] == "r2"][-1]
    assert last_r2["action"] == "keep"
    assert last_r2["label"] == "Family"


def test_classify_missing_decision_defaults_to_keep(tmp_path, patched,
                                                   fake_thread, decisions_json):
    """If the LLM never returns a decision for an id, after all retries
    the safe default is keep-no-label — never trash."""
    patched["threads"] = [fake_thread(tid="m1"), fake_thread(tid="m2")]
    # Model only returns m1's decision; m2 is silently dropped.
    patched["backend_responses"] = [
        decisions_json([{"id": "m1", "action": "trash", "label": None}]),
        # All retries also miss m2
        decisions_json([]),
        decisions_json([]),
        decisions_json([]),
    ]
    result = _invoke_classify(tmp_path, [
        "--dry-run", "--concurrency", "1",
        "--llm-retries", "1", "--batch-size", "5",
    ])
    assert result.exit_code == 0, result.output

    rows = _read_log(tmp_path / "dry-run.log")
    by_id = {r["id"]: r for r in rows}
    assert by_id["m2"]["action"] == "keep"
    assert by_id["m2"]["label"] is None
