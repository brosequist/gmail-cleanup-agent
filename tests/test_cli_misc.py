"""Workflow tests that don't fit cleanly into the per-subcommand files.

  - `auth` subcommand is registered
  - `cli --verbose` raises the root logger to DEBUG
  - classify when backend raises → all decisions logged as action='error'
  - classify with concurrency > 1 and multi-batch flushing still produces
    correct decisions
  - classify --retry-errors when the log file doesn't exist (graceful no-op)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from gmail_cleanup import cli as cli_module
from gmail_cleanup.cli import cli


def test_auth_subcommand_registered():
    """auth requires a browser and can't be unit-tested end-to-end, but
    we want a regression test that it's wired into the CLI group."""
    assert "auth" in cli.commands
    assert cli.commands["auth"].name == "auth"


def test_verbose_flag_raises_log_level():
    """`-v / --verbose` should bump the root logger to DEBUG.

    Tested directly via setup_logging — driving it through `cli --help`
    doesn't reliably run the group callback before Click's --help
    handler short-circuits, depending on Click version.

    `logging.basicConfig` is a no-op when the root logger already has
    handlers (which previous tests in this process will have added), so
    we clear them first to give setup_logging a clean slate.
    """
    from gmail_cleanup.cli import setup_logging

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    try:
        root.handlers.clear()
        setup_logging(verbose=True)
        assert root.level == logging.DEBUG

        root.handlers.clear()
        setup_logging(verbose=False)
        assert root.level == logging.INFO
    finally:
        # Restore previous handlers so later tests aren't affected
        root.handlers.clear()
        root.handlers.extend(saved_handlers)


@pytest.fixture
def patched_classify(monkeypatch, patch_repo_root):
    """Same shape as the fixture in test_cli_classify but local so the
    files don't have to share fixtures via conftest."""
    from tests._fakes import FakeBackend, FakeGmailClient

    state = {"threads": [], "backend_responses": []}

    def factory():
        gc = FakeGmailClient(threads_to_yield=state["threads"])
        state["client"] = gc
        return gc

    def get_backend():
        # Allow tests to install a backend instance directly OR list of
        # responses. Direct backend wins.
        if "backend" in state and state["backend"] is not None:
            return state["backend"]
        b = FakeBackend(responses=state["backend_responses"])
        state["backend"] = b
        return b

    monkeypatch.setattr(cli_module, "_client", factory)
    monkeypatch.setattr(cli_module, "get_backend", get_backend)
    return state


def _invoke(tmp_path, args):
    runner = CliRunner()
    return runner.invoke(cli, [
        "classify",
        "--state-file", str(tmp_path / "state.json"),
        "--log-file", str(tmp_path / "dry-run.log"),
        *args,
    ])


def _read_log(p: Path) -> list[dict]:
    out: list[dict] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("==="):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def test_classify_backend_error_logs_error_action(tmp_path, patched_classify,
                                                  fake_thread):
    """When the backend raises on the FIRST call, every email in the
    batch is logged with action='error'. Critical for the
    `--retry-errors` workflow — it relies on these rows existing."""
    class ExplodingBackend:
        def classify_batch(self, prompt):
            raise RuntimeError("ollama unreachable")
    patched_classify["threads"] = [
        fake_thread(tid="e1"), fake_thread(tid="e2"),
    ]
    patched_classify["backend"] = ExplodingBackend()

    result = _invoke(tmp_path, [
        "--dry-run", "--concurrency", "1",
        "--llm-retries", "0",   # no retry — first failure is final
    ])
    assert result.exit_code == 0, result.output

    rows = _read_log(tmp_path / "dry-run.log")
    by_id = {r["id"]: r for r in rows}
    assert by_id["e1"]["action"] == "error"
    assert by_id["e2"]["action"] == "error"
    assert "ollama unreachable" in by_id["e1"]["note"]


def test_classify_concurrency_and_multibatch(tmp_path, patched_classify,
                                             fake_thread):
    """Concurrency > 1 + a batch buffer that flushes multiple times. We
    use a backend that hands back per-call canned responses so each
    sub-batch gets its own decisions."""
    from tests._fakes import FakeBackend
    patched_classify["threads"] = [fake_thread(tid=f"c{i}") for i in range(8)]
    patched_classify["backend"] = FakeBackend(responses=[
        # batch 1 (c0..c1)
        json.dumps({"decisions": [
            {"id": "c0", "action": "trash", "label": None},
            {"id": "c1", "action": "trash", "label": None},
        ]}),
        # batch 2 (c2..c3)
        json.dumps({"decisions": [
            {"id": "c2", "action": "keep", "label": "Receipts"},
            {"id": "c3", "action": "trash", "label": None},
        ]}),
        # batch 3 (c4..c5)
        json.dumps({"decisions": [
            {"id": "c4", "action": "trash", "label": None},
            {"id": "c5", "action": "keep", "label": "Family"},
        ]}),
        # batch 4 (c6..c7)
        json.dumps({"decisions": [
            {"id": "c6", "action": "trash", "label": None},
            {"id": "c7", "action": "trash", "label": None},
        ]}),
    ])

    result = _invoke(tmp_path, [
        "--dry-run", "--concurrency", "2", "--batch-size", "2",
    ])
    assert result.exit_code == 0, result.output

    rows = _read_log(tmp_path / "dry-run.log")
    by_id = {r["id"]: r for r in rows}
    # All 8 threads got classified
    assert set(by_id) == {f"c{i}" for i in range(8)}
    # Specific assertions to make sure decisions weren't scrambled
    assert by_id["c2"]["action"] == "keep" and by_id["c2"]["label"] == "Receipts"
    assert by_id["c5"]["action"] == "keep" and by_id["c5"]["label"] == "Family"
    assert by_id["c0"]["action"] == "trash"


def test_classify_retry_errors_with_no_log_file_is_graceful(tmp_path,
                                                            patched_classify,
                                                            fake_thread):
    """`--retry-errors` should not crash when the requested log file
    doesn't exist — the run should proceed normally."""
    patched_classify["threads"] = [fake_thread(tid="g1")]
    patched_classify["backend_responses"] = [json.dumps({"decisions": [
        {"id": "g1", "action": "trash", "label": None},
    ]})]

    log_path = tmp_path / "fresh.log"  # does NOT exist
    assert not log_path.exists()

    runner = CliRunner()
    result = runner.invoke(cli, [
        "classify", "--dry-run", "--concurrency", "1",
        "--state-file", str(tmp_path / "state.json"),
        "--log-file", str(log_path),
        "--retry-errors",
    ])
    assert result.exit_code == 0, result.output
    # Log was written normally
    rows = _read_log(log_path)
    assert {r["id"] for r in rows} == {"g1"}
