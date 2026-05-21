"""End-to-end tests for the `classify` subcommand.

Exercises the Click CLI with mocked GmailClient + backend. Verifies:
  - dry-run produces decision log + state.json but never mutates Gmail
  - --apply triggers trash_thread / modify_thread_labels
  - --limit caps the number of processed threads
  - --include-body flows through to search_threads
  - --console-log mirrors stderr to a file
  - --retry-errors re-classifies threads previously marked "error"
  - whitelisted senders short-circuit the LLM entirely
  - --reviewed-label labels kept + whitelisted mail and filters the query
  - --skip-label excludes additional labels from the query
  - a failed trash/label apply is logged as action="error", not a
    silent keep/trash
  - --reviewed-label resolves case-insensitively against existing labels
  - nested label names work; names with a double-quote are excluded
    from the skip filter with a warning
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
        gc = FakeGmailClient(threads_to_yield=state["threads"],
                             labels=state.get("labels"))
        gc.fail_on = set(state.get("fail_on", ()))
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


def _read_counters(p: Path) -> dict:
    """Pull the final '=== done. counters: {...} ===' summary from a log."""
    for line in reversed(p.read_text().splitlines()):
        if line.startswith("=== done. counters:"):
            body = (line.removeprefix("=== done. counters: ")
                        .removesuffix(" ==="))
            return json.loads(body)
    return {}


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
    # t1 got the Receipts label (Label_1 in the fake) via threads.modify
    mods = {m["id"]: m for m in patched["client"].modified}
    assert mods["t1"]["add"] == ["Label_1"]


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


# ---------------- --reviewed-label / --skip-label ----------------


def test_classify_reviewed_label_off_by_default(tmp_path, patched, decisions_json):
    """Without the flag: query is untouched, no reviewed label is created,
    and the log carries no reviewed_label field."""
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--apply", "--concurrency", "1", "--confirm-every", "0", "--query", "q",
    ])
    assert result.exit_code == 0, result.output

    assert patched["client"].last_query == "q"
    assert patched["client"].created_labels == []
    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert "reviewed_label" not in rows["t1"]
    # keep still labels normally
    mods = {m["id"]: m for m in patched["client"].modified}
    assert mods["t1"]["add"] == ["Label_1"]


def test_classify_reviewed_label_default_name(tmp_path, patched, decisions_json):
    """Bare --reviewed-label uses the name 'Reviewed': it is created,
    applied to kept mail alongside the category label, kept off trashed
    mail, and appended to the Gmail query as a skip filter."""
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--apply", "--concurrency", "1", "--confirm-every", "0",
        "--query", "anything", "--reviewed-label",
    ])
    assert result.exit_code == 0, result.output

    assert "Reviewed" in patched["client"].created_labels
    reviewed_id = patched["client"]._labels["Reviewed"]

    # t1 (kept) got Receipts + Reviewed in a single modify
    mods = {m["id"]: m for m in patched["client"].modified}
    assert set(mods["t1"]["add"]) == {"Label_1", reviewed_id}
    # t2 (trashed) was trashed, never labeled
    assert patched["client"].trashed == ["t2"]
    assert "t2" not in mods

    # skip filter appended to the query
    assert '-label:"Reviewed"' in patched["client"].last_query

    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["t1"]["reviewed_label"] == "Reviewed"
    assert "reviewed_label" not in rows["t2"]


def test_classify_reviewed_label_custom_name(tmp_path, patched, decisions_json):
    """--reviewed-label=NAME uses the custom name, including spaces."""
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "keep", "label": "Family"},
    ])]
    result = _invoke_classify(tmp_path, [
        "--dry-run", "--concurrency", "1", "--query", "q",
        "--reviewed-label=LLM Reviewed",
    ])
    assert result.exit_code == 0, result.output

    assert '-label:"LLM Reviewed"' in patched["client"].last_query
    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["t1"]["reviewed_label"] == "LLM Reviewed"
    assert rows["t2"]["reviewed_label"] == "LLM Reviewed"


def test_classify_reviewed_label_dry_run_no_mutation(tmp_path, patched,
                                                     decisions_json):
    """--reviewed-label under --dry-run logs intent but mutates nothing."""
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--dry-run", "--concurrency", "1", "--query", "q", "--reviewed-label",
    ])
    assert result.exit_code == 0, result.output

    assert patched["client"].modified == []
    assert patched["client"].trashed == []
    assert patched["client"].created_labels == []
    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["t1"]["reviewed_label"] == "Reviewed"
    assert rows["t1"]["note"] == ""  # dry-run → not actually applied


def test_classify_reviewed_label_applies_to_whitelisted(tmp_path, patched,
                                                        fake_thread, config_dir,
                                                        decisions_json):
    """Whitelisted mail bypasses the LLM but still gets the reviewed label."""
    patched["threads"] = [
        fake_thread(tid="w1", sender="trusted@example-keep.com"),
        fake_thread(tid="t2", sender="random@spam.example"),
    ]
    patched["backend_responses"] = [decisions_json([
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--apply", "--concurrency", "1", "--confirm-every", "0",
        "--query", "q", "--reviewed-label", "--batch-size", "5",
    ])
    assert result.exit_code == 0, result.output

    reviewed_id = patched["client"]._labels["Reviewed"]
    mods = {m["id"]: m for m in patched["client"].modified}
    assert mods["w1"]["add"] == [reviewed_id]

    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["w1"]["note"] == "whitelist"
    assert rows["w1"]["reviewed_label"] == "Reviewed"


def test_classify_skip_label_repeatable(tmp_path, patched, decisions_json):
    """--skip-label is repeatable; each name becomes a query exclusion."""
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "trash", "label": None},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--dry-run", "--concurrency", "1", "--query", "base",
        "--skip-label", "Archived", "--skip-label", "Do Not Touch",
    ])
    assert result.exit_code == 0, result.output

    q = patched["client"].last_query
    assert '-label:"Archived"' in q
    assert '-label:"Do Not Touch"' in q
    # no reviewed label was set, so nothing is created
    assert patched["client"].created_labels == []


def test_classify_reviewed_label_collision_warns(tmp_path, patched, caplog,
                                                 decisions_json):
    """A --reviewed-label name that collides with a labels.yaml category
    logs a warning but the run still proceeds normally."""
    import logging

    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    with caplog.at_level(logging.WARNING, logger="gmail_cleanup"):
        result = _invoke_classify(tmp_path, [
            "--dry-run", "--concurrency", "1", "--query", "q",
            "--reviewed-label=Receipts",
        ])
    assert result.exit_code == 0, result.output
    assert "also a category label" in caplog.text

    # collision is a warning only — the run still labels normally
    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["t1"]["reviewed_label"] == "Receipts"


def test_classify_reviewed_label_no_collision_is_quiet(tmp_path, patched,
                                                       caplog, decisions_json):
    """A distinct --reviewed-label name produces no collision warning."""
    import logging

    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    with caplog.at_level(logging.WARNING, logger="gmail_cleanup"):
        result = _invoke_classify(tmp_path, [
            "--dry-run", "--concurrency", "1", "--query", "q",
            "--reviewed-label=Reviewed",
        ])
    assert result.exit_code == 0, result.output
    assert "also a category label" not in caplog.text


# ---------------- apply-failure -> error ----------------


def test_classify_apply_label_failure_logs_error(tmp_path, patched,
                                                 decisions_json):
    """If a kept email's label modify raises, the row is logged as
    action='error' and counted as an error — never a silent 'keep'."""
    patched["fail_on"] = {"t1"}
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--apply", "--concurrency", "1", "--confirm-every", "0", "--query", "q",
    ])
    assert result.exit_code == 0, result.output

    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["t1"]["action"] == "error"
    assert "label apply failed" in rows["t1"]["note"]
    # t2 unaffected
    assert rows["t2"]["action"] == "trash"
    assert patched["client"].trashed == ["t2"]

    # counted once as an error, not also as a keep
    counters = _read_counters(tmp_path / "dry-run.log")
    assert counters["errors"] == 1
    assert counters["keep"] == 0
    assert counters["trash"] == 1


def test_classify_apply_trash_failure_logs_error(tmp_path, patched,
                                                 decisions_json):
    """A failed trash call is logged as action='error', not 'trash'."""
    patched["fail_on"] = {"t2"}
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--apply", "--concurrency", "1", "--confirm-every", "0", "--query", "q",
    ])
    assert result.exit_code == 0, result.output

    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["t2"]["action"] == "error"
    assert "trash failed" in rows["t2"]["note"]
    assert rows["t1"]["action"] == "keep"

    counters = _read_counters(tmp_path / "dry-run.log")
    assert counters["errors"] == 1
    assert counters["trash"] == 0


def test_classify_reviewed_label_whitelist_apply_failure_logs_error(
        tmp_path, patched, fake_thread, config_dir, decisions_json):
    """A failed reviewed-label apply on a whitelisted email marks the
    whole record as error rather than a successful 'whitelist' keep."""
    patched["threads"] = [
        fake_thread(tid="w1", sender="trusted@example-keep.com"),
        fake_thread(tid="t2", sender="random@spam.example"),
    ]
    patched["fail_on"] = {"w1"}
    patched["backend_responses"] = [decisions_json([
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--apply", "--concurrency", "1", "--confirm-every", "0",
        "--query", "q", "--reviewed-label", "--batch-size", "5",
    ])
    assert result.exit_code == 0, result.output

    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["w1"]["action"] == "error"
    assert "reviewed-label apply failed" in rows["w1"]["note"]

    counters = _read_counters(tmp_path / "dry-run.log")
    assert counters["errors"] == 1
    assert counters["whitelist"] == 0


# ---------------- case-insensitive label resolution ----------------


def test_classify_reviewed_label_case_insensitive_reuses_existing(
        tmp_path, patched, decisions_json):
    """--reviewed-label that differs only in casing from an existing Gmail
    label reuses that label instead of creating a near-duplicate."""
    patched["labels"] = {"Receipts": "Label_1", "Family": "Label_2",
                         "Reviewed": "Label_99"}
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--apply", "--concurrency", "1", "--confirm-every", "0",
        "--query", "q", "--reviewed-label=reVIEWed",
    ])
    assert result.exit_code == 0, result.output

    # existing "Reviewed" reused — no near-duplicate created
    assert patched["client"].created_labels == []
    mods = {m["id"]: m for m in patched["client"].modified}
    assert set(mods["t1"]["add"]) == {"Label_1", "Label_99"}
    # canonical existing name recorded in the log
    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["t1"]["reviewed_label"] == "Reviewed"


def test_classify_reviewed_label_collision_is_case_insensitive(
        tmp_path, patched, caplog, decisions_json):
    """The labels.yaml collision warning is case-insensitive — Gmail label
    search is case-insensitive, so a case variant still collides."""
    import logging

    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    with caplog.at_level(logging.WARNING, logger="gmail_cleanup"):
        result = _invoke_classify(tmp_path, [
            "--dry-run", "--concurrency", "1", "--query", "q",
            "--reviewed-label=RECEIPTS",
        ])
    assert result.exit_code == 0, result.output
    assert "also a category label" in caplog.text


# ---------------- nested names + quotes ----------------


def test_classify_reviewed_label_nested_name(tmp_path, patched, decisions_json):
    """A nested 'Parent/Child' reviewed-label name flows through cleanly:
    quoted into the skip query, created, and applied."""
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--apply", "--concurrency", "1", "--confirm-every", "0",
        "--query", "q", "--reviewed-label=Cleanup/LLM Reviewed",
    ])
    assert result.exit_code == 0, result.output

    assert '-label:"Cleanup/LLM Reviewed"' in patched["client"].last_query
    assert "Cleanup/LLM Reviewed" in patched["client"].created_labels
    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["t1"]["reviewed_label"] == "Cleanup/LLM Reviewed"


def test_classify_skip_label_nested_name(tmp_path, patched, decisions_json):
    """A nested skip-label name is quoted into the query unchanged."""
    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "trash", "label": None},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    result = _invoke_classify(tmp_path, [
        "--dry-run", "--concurrency", "1", "--query", "base",
        "--skip-label", "Archive/2025",
    ])
    assert result.exit_code == 0, result.output
    assert '-label:"Archive/2025"' in patched["client"].last_query


def test_classify_reviewed_label_with_quote_omits_skip_filter(
        tmp_path, patched, caplog, decisions_json):
    """A reviewed-label name with a double-quote can't be a Gmail query
    term: classify warns, leaves the query un-malformed, but still
    creates and applies the label."""
    import logging

    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "keep", "label": "Receipts"},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    with caplog.at_level(logging.WARNING, logger="gmail_cleanup"):
        result = _invoke_classify(tmp_path, [
            "--apply", "--concurrency", "1", "--confirm-every", "0",
            "--query", "q", '--reviewed-label=My "Reviewed" Tag',
        ])
    assert result.exit_code == 0, result.output

    # query untouched — the un-expressible name was dropped, not emitted
    assert patched["client"].last_query == "q"
    assert "double-quote" in caplog.text
    # the label itself is still created + applied
    assert 'My "Reviewed" Tag' in patched["client"].created_labels
    rows = {r["id"]: r for r in _read_log(tmp_path / "dry-run.log")}
    assert rows["t1"]["reviewed_label"] == 'My "Reviewed" Tag'


def test_classify_skip_label_with_quote_omits_filter(tmp_path, patched,
                                                     caplog, decisions_json):
    """A skip-label name with a double-quote is dropped (with a warning)
    rather than producing a malformed Gmail query."""
    import logging

    patched["backend_responses"] = [decisions_json([
        {"id": "t1", "action": "trash", "label": None},
        {"id": "t2", "action": "trash", "label": None},
    ])]
    with caplog.at_level(logging.WARNING, logger="gmail_cleanup"):
        result = _invoke_classify(tmp_path, [
            "--dry-run", "--concurrency", "1", "--query", "base",
            "--skip-label", 'Weird "Label"',
        ])
    assert result.exit_code == 0, result.output
    assert patched["client"].last_query == "base"
    assert "double-quote" in caplog.text
