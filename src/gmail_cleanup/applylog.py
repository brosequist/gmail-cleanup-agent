"""Replay decision-log entries to Gmail without re-classifying.

Reads a decision log (e.g. dry-run.log), keeps the latest decision per
thread ID, and replays each one via Gmail's batch HTTP endpoint:

  - action="trash"            -> users().threads().trash(...)
  - action="keep", label=X    -> users().threads().modify(addLabelIds=[X])
  - action="keep", label=None -> no Gmail call (already kept)
  - action="error"            -> skipped (never applied)

Single-threaded — the existing GmailClient is not thread-safe (see
cli._apply_decisions), and concurrent use can segfault httplib2.

Resumable: applied IDs are checkpointed to a state file after each
batch. Re-runs skip already-applied IDs. Two audit logs:
  --dry-run -> replay-preview.log
  --apply   -> applied.log

429s from Gmail's per-user concurrent limit are retried per batch with
exponential backoff; persistent failures fall through as errors.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from collections import Counter
from pathlib import Path

from .gmail_client import GmailClient

logger = logging.getLogger("gmail_cleanup.applylog")

BATCH_SIZE = 20  # Sub-requests per batch. Gmail's batch endpoint accepts up
                 # to 100 but per-user concurrent ceiling is ~20; larger
                 # batches add 429s without improving the ~3.3 ops/sec ceiling.
MAX_429_RETRIES = 5


def load_latest_decisions(log_path: Path) -> dict[str, dict]:
    """For each thread ID, return the LATEST decision record from the log."""
    latest: dict[str, dict] = {}
    with log_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith('{"id":'):
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            latest[r["id"]] = r
    return latest


def _execute_with_retry(service, items, label_ids, audit_fh, counters,
                        decisions_by_id, batch_idx):
    """Execute a batch of trash/modify requests with 429-retry. Returns the
    set of IDs that completed successfully. Persistent 429s after
    MAX_429_RETRIES are logged as errors."""
    successful: set[str] = set()
    pending = list(items)
    for attempt in range(MAX_429_RETRIES + 1):
        if not pending:
            break
        retry: list[dict] = []

        def cb(request_id, response, exception, _retry=retry):
            if exception is None:
                successful.add(request_id)
                d = decisions_by_id[request_id]
                if d["action"] == "trash":
                    counters["trash"] += 1
                    result = "trash"
                else:
                    counters["keep_labeled"] += 1
                    result = "keep_labeled"
                audit_fh.write(json.dumps({
                    "id": request_id, "result": result,
                    "label": d.get("label")}) + "\n")
                return
            err = str(exception)
            if "429" in err or "rate" in err.lower() or "concurrent" in err.lower():
                _retry.append(decisions_by_id[request_id])
            else:
                counters["error"] += 1
                audit_fh.write(json.dumps({
                    "id": request_id, "result": "error",
                    "err": err[:200]}) + "\n")

        batch = service.new_batch_http_request(callback=cb)
        for d in pending:
            tid = d["id"]
            if d["action"] == "trash":
                req = service.users().threads().trash(userId="me", id=tid)
            else:
                req = service.users().threads().modify(
                    userId="me", id=tid,
                    body={"addLabelIds": [label_ids[d["label"]]]})
            batch.add(req, request_id=tid)
        try:
            batch.execute()
        except Exception as e:  # noqa: BLE001
            logger.error("batch %d transport failed on attempt %d: %s",
                         batch_idx, attempt + 1, e)
            retry = pending[:]

        if retry:
            sleep_for = 1.5 * (2 ** attempt)  # 1.5, 3, 6, 12, 24, 48
            logger.warning("  batch %d: %d items hit 429 on attempt %d, "
                           "backing off %.1fs", batch_idx, len(retry),
                           attempt + 1, sleep_for)
            time.sleep(sleep_for)
        pending = retry

    for d in pending:
        counters["error"] += 1
        audit_fh.write(json.dumps({
            "id": d["id"], "result": "error",
            "err": f"429 after {MAX_429_RETRIES} retries"}) + "\n")
    return successful


def run_apply_log(
    *,
    log_file: Path,
    state_file: Path,
    apply: bool,
    limit: int | None,
    batch_size: int,
    batch_sleep: float,
    credentials: Path,
    token: Path,
    audit_log: Path,
) -> None:
    """Replay decisions from `log_file` to Gmail.

    Behaviour mirrors the old scripts/apply_from_log.py. `audit_log`
    receives one JSON record per replayed decision; `state_file` tracks
    applied IDs for resume.
    """
    mode = "APPLY (mutating Gmail)" if apply else "dry-run (no mutations)"
    logger.info("mode: %s", mode)
    logger.info("log file: %s", log_file)
    logger.info("audit log: %s", audit_log)

    logger.info("loading decisions...")
    decisions = load_latest_decisions(log_file)
    actions = Counter(d["action"] for d in decisions.values())
    logger.info("loaded %d unique thread decisions: %s",
                len(decisions), dict(actions))

    applied_ids: set[str] = set()
    if state_file.exists():
        try:
            applied_ids = set(json.loads(state_file.read_text()).get("applied", []))
            logger.info("resume: %d already applied per %s",
                        len(applied_ids), state_file.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not read %s; starting fresh: %s", state_file, e)

    pending = [
        d for tid, d in decisions.items()
        if tid not in applied_ids and d["action"] in ("trash", "keep")
    ]
    if limit is not None:
        pending = pending[:limit]
    logger.info("pending actions: %d (after resume + --limit)", len(pending))

    if not pending:
        logger.info("nothing to do; exiting")
        return

    client = GmailClient(credentials, token)
    client.authorize()
    service = client._service  # noqa: SLF001
    label_ids: dict[str, str] = client.list_labels()

    needed_labels = {
        d.get("label") for d in pending
        if d["action"] == "keep" and d.get("label")
    }
    missing = sorted(needed_labels - set(label_ids.keys()))
    if missing:
        if apply:
            for name in missing:
                logger.info("creating label %r", name)
                label_ids[name] = client.create_label(name)
        else:
            for name in missing:
                label_ids[name] = f"<would-create:{name}>"
            logger.info("[dry-run] would create %d new label(s): %s",
                        len(missing), missing)

    audit_fh = audit_log.open("a")
    audit_fh.write(
        f"\n=== {_dt.datetime.now(_dt.timezone.utc).isoformat()} "
        f"apply-log starting (apply={apply}, limit={limit}) ===\n")
    audit_fh.flush()

    counters: Counter[str] = Counter()
    start = time.time()
    decisions_by_id = {d["id"]: d for d in pending}

    chunks = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    total = len(pending)

    try:
        for batch_idx, chunk in enumerate(chunks, 1):
            to_apply: list[dict] = []
            for d in chunk:
                tid = d["id"]
                action = d["action"]
                label = d.get("label")
                if action == "keep" and not label:
                    counters["keep_nolabel"] += 1
                    audit_fh.write(json.dumps(
                        {"id": tid, "result": "keep_nolabel", "label": None}) + "\n")
                    if apply:
                        applied_ids.add(tid)
                    continue
                if not apply:
                    if action == "trash":
                        counters["trash"] += 1
                        audit_fh.write(json.dumps(
                            {"id": tid, "result": "trash", "label": None}) + "\n")
                    else:
                        counters["keep_labeled"] += 1
                        audit_fh.write(json.dumps(
                            {"id": tid, "result": "keep_labeled", "label": label}) + "\n")
                    continue
                if action == "keep" and label and not label_ids.get(label):
                    counters["error"] += 1
                    audit_fh.write(json.dumps(
                        {"id": tid, "result": "error",
                         "err": f"label {label!r} not resolved"}) + "\n")
                    continue
                to_apply.append(d)

            if to_apply:
                successful = _execute_with_retry(
                    service, to_apply, label_ids, audit_fh, counters,
                    decisions_by_id, batch_idx)
                if apply:
                    applied_ids.update(successful)

            audit_fh.flush()
            if apply:
                state_file.write_text(json.dumps({"applied": sorted(applied_ids)}))

            done = min(batch_idx * batch_size, total)
            elapsed = time.time() - start
            rate = done / elapsed if elapsed > 0 else 0
            eta_min = int((total - done) / rate / 60) if rate > 0 else 0
            if batch_idx % 5 == 0 or batch_idx == len(chunks):
                logger.info(
                    "batch %d/%d (%d/%d, %.1f%%) | %.1f ops/sec | ETA %d min | %s",
                    batch_idx, len(chunks), done, total,
                    100 * done / total, rate, eta_min, dict(counters))

            if apply and batch_idx < len(chunks):
                time.sleep(batch_sleep)
    finally:
        audit_fh.flush()
        audit_fh.close()
        if apply:
            state_file.write_text(json.dumps({"applied": sorted(applied_ids)}))

    elapsed = time.time() - start
    logger.info("DONE in %.1f min (%.1f ops/sec). mode=%s counters=%s",
                elapsed / 60, total / elapsed if elapsed else 0,
                mode, dict(counters))
