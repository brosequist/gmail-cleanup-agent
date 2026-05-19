"""Tests for src/gmail_cleanup/applylog.py.

Covers:
  - load_latest_decisions: latest-per-id, skip headers, skip malformed
  - _execute_with_retry:  success / 429-retry / non-retryable error paths
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from gmail_cleanup.applylog import (
    MAX_429_RETRIES,
    _execute_with_retry,
    load_latest_decisions,
)


# ---------------- load_latest_decisions ----------------


def test_load_latest_decisions_keeps_latest_per_id(tmp_path: Path):
    log = tmp_path / "dry-run.log"
    log.write_text(
        '\n=== run header ignored ===\n'
        + '{"id":"a","action":"keep","label":"Receipts"}\n'
        + '{"id":"b","action":"trash","label":null}\n'
        + 'malformed line that is not json\n'
        + '{"id":"a","action":"trash","label":null}\n'   # a re-classified
        + '{"id":"c","action":"keep","label":"Family"}\n'
    )
    decs = load_latest_decisions(log)
    # a's *latest* should win — trash, not keep
    assert decs["a"]["action"] == "trash"
    assert decs["b"]["action"] == "trash"
    assert decs["c"]["label"] == "Family"
    assert "===" not in decs  # header line ignored
    assert len(decs) == 3


def test_load_latest_decisions_empty_file(tmp_path: Path):
    log = tmp_path / "empty.log"
    log.write_text("")
    assert load_latest_decisions(log) == {}


# ---------------- _execute_with_retry ----------------


class _BatchExec:
    """Programmable fake batch. Each call to execute() pops the next
    scripted callback result-map: {tid: (response, exception)}."""

    def __init__(self, scripts):
        self._scripts = list(scripts)  # list of dicts per execute() call
        self._calls = 0

    def make(self, callback):
        outer = self

        class _B:
            def __init__(self):
                self._items = []

            def add(self, _req, request_id):
                self._items.append(request_id)

            def execute(_self):  # noqa: N805
                script = outer._scripts[outer._calls]
                outer._calls += 1
                for rid in _self._items:
                    resp, exc = script.get(rid, (None, None))
                    callback(rid, resp, exc)
        return _B()


class _FakeService:
    def __init__(self, batch_exec: _BatchExec):
        self._batch_exec = batch_exec
        self.trashed = []
        self.modified = []

    def new_batch_http_request(self, callback):
        return self._batch_exec.make(callback)

    def users(self):
        return self

    def threads(self):
        return self

    def trash(self, *, userId, id):  # noqa: A002
        self.trashed.append(id)
        return object()

    def modify(self, *, userId, id, body):  # noqa: A002
        self.modified.append((id, body))
        return object()


def test_execute_with_retry_all_succeed(tmp_path: Path):
    items = [
        {"id": "t1", "action": "trash"},
        {"id": "k1", "action": "keep", "label": "Receipts"},
    ]
    label_ids = {"Receipts": "Label_1"}
    counters: Counter[str] = Counter()
    audit = (tmp_path / "audit.log").open("w")
    decisions_by_id = {d["id"]: d for d in items}
    bx = _BatchExec([{"t1": (None, None), "k1": (None, None)}])
    svc = _FakeService(bx)

    successful = _execute_with_retry(
        svc, items, label_ids, audit, counters, decisions_by_id, batch_idx=1,
    )
    audit.close()

    assert successful == {"t1", "k1"}
    assert counters == Counter(trash=1, keep_labeled=1)
    assert svc.trashed == ["t1"]
    assert svc.modified == [("k1", {"addLabelIds": ["Label_1"]})]


def test_execute_with_retry_429_then_succeed(tmp_path: Path, monkeypatch):
    """First batch hits a 429 on one item; retry succeeds. Sleep is
    monkeypatched away so the test is fast."""
    monkeypatch.setattr("gmail_cleanup.applylog.time.sleep", lambda _s: None)

    items = [{"id": "x", "action": "trash"}]
    counters: Counter[str] = Counter()
    audit = (tmp_path / "audit.log").open("w")
    by_id = {d["id"]: d for d in items}

    fail = Exception("HTTP 429 Too Many Concurrent Requests")
    bx = _BatchExec([
        {"x": (None, fail)},   # attempt 1: 429
        {"x": (None, None)},   # attempt 2: success
    ])
    svc = _FakeService(bx)

    successful = _execute_with_retry(svc, items, {}, audit, counters, by_id, 1)
    audit.close()

    assert successful == {"x"}
    assert counters["trash"] == 1
    # Two batch executions: the first hit 429, the second succeeded.
    assert bx._calls == 2


def test_execute_with_retry_non_retryable_error(tmp_path: Path):
    """A 4xx other than 429 should NOT retry — the item gets logged as an
    error on the first attempt."""
    items = [{"id": "x", "action": "trash"}]
    counters: Counter[str] = Counter()
    audit_path = tmp_path / "audit.log"
    audit = audit_path.open("w")
    by_id = {d["id"]: d for d in items}

    fail = Exception("HTTP 400 Bad Request — bogus thread")
    bx = _BatchExec([{"x": (None, fail)}])
    svc = _FakeService(bx)

    successful = _execute_with_retry(svc, items, {}, audit, counters, by_id, 1)
    audit.close()

    assert successful == set()
    assert counters["error"] == 1
    line = audit_path.read_text().strip()
    rec = json.loads(line)
    assert rec["id"] == "x" and rec["result"] == "error"
    assert "400" in rec["err"]


def test_execute_with_retry_persistent_429_falls_through(tmp_path: Path, monkeypatch):
    """After MAX_429_RETRIES, the item is logged as an error rather than
    looping forever."""
    monkeypatch.setattr("gmail_cleanup.applylog.time.sleep", lambda _s: None)

    items = [{"id": "x", "action": "trash"}]
    counters: Counter[str] = Counter()
    audit = (tmp_path / "audit.log").open("w")
    by_id = {d["id"]: d for d in items}

    fail = Exception("rate limit exceeded")
    # Every attempt fails with a retryable 429.
    bx = _BatchExec([{"x": (None, fail)}] * (MAX_429_RETRIES + 1))
    svc = _FakeService(bx)

    successful = _execute_with_retry(svc, items, {}, audit, counters, by_id, 1)
    audit.close()

    assert successful == set()
    assert counters["error"] == 1
