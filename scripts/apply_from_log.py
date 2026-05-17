#!/usr/bin/env python3
"""Apply dry-run.log decisions to Gmail without re-classifying.

Reads dry-run.log, keeps the latest decision per thread ID, and replays
each decision via the Gmail batch HTTP API:

  - action="trash"            -> users().threads().trash(...)
  - action="keep", label=X    -> users().threads().modify(addLabelIds=[X])
  - action="keep", label=None -> no-op (already kept; nothing to change)
  - action="error"            -> skipped (never applied)

Uses Gmail's batch HTTP endpoint (up to 100 sub-requests per HTTP call).
Single-threaded — the existing GmailClient is not thread-safe (see
cli.py:_apply_decisions), and concurrent use can segfault httplib2.

Resumable: applied IDs are checkpointed to state-applied.json after each
batch. Re-runs skip already-applied IDs. Two log files:

  --dry-run mode -> writes the would-do preview to replay-preview.log
  --apply mode   -> writes the actual audit trail to applied.log
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

import click

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from gmail_cleanup.gmail_client import GmailClient  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("apply_from_log")

BATCH_SIZE = 25  # Per-batch sub-requests. Gmail's batch HTTP supports up to
                 # 100 but processes them concurrently server-side; the
                 # per-user concurrent-request ceiling is around 20, so 25
                 # plus retry-on-429 hits the sweet spot.
MAX_429_RETRIES = 5  # Per-batch retry budget for transient 429s


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
    """Execute a batch of trash/modify requests. Returns set of IDs that
    successfully completed. 429s are retried with exponential backoff up to
    MAX_429_RETRIES; persistent failures are logged as errors."""
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

    # Anything still pending after MAX_429_RETRIES is a permanent failure
    for d in pending:
        counters["error"] += 1
        audit_fh.write(json.dumps({
            "id": d["id"], "result": "error",
            "err": f"429 after {MAX_429_RETRIES} retries"}) + "\n")
    return successful


@click.command(context_settings={"max_content_width": 100})
@click.option("--log-file", type=click.Path(exists=True, path_type=Path),
              default=REPO_ROOT / "dry-run.log", show_default=True,
              help="Source of decisions to replay.")
@click.option("--state-file", type=click.Path(path_type=Path),
              default=REPO_ROOT / "state-applied.json", show_default=True,
              help="Tracks applied IDs across runs for resume.")
@click.option("--apply/--dry-run", default=False, show_default=True,
              help="--apply actually mutates Gmail. Default is dry-run preview.")
@click.option("--limit", type=int, default=None,
              help="Apply at most this many actions (for staged rollout).")
@click.option("--batch-sleep", type=float, default=1.5, show_default=True,
              help="Sleep between batches (seconds). Each batch is up to 100 "
                   "sub-requests at 5 QU each; Gmail limit is 250 QU/s/user, "
                   "so 1.5s keeps us comfortably under quota.")
@click.option("--credentials", type=click.Path(exists=True, path_type=Path),
              default=REPO_ROOT / "config" / "credentials.json", show_default=True)
@click.option("--token", type=click.Path(path_type=Path),
              default=REPO_ROOT / "config" / "token.json", show_default=True)
def main(log_file, state_file, apply, limit, batch_sleep, credentials, token):
    mode = "APPLY (mutating Gmail)" if apply else "dry-run (no mutations)"
    logger.info("mode: %s", mode)
    logger.info("log file: %s", log_file)

    # Per-mode audit log so apply records aren't mixed with dry-run previews.
    audit_log = REPO_ROOT / ("applied.log" if apply else "replay-preview.log")
    logger.info("audit log: %s", audit_log)

    # 1. Load latest-per-ID decisions from dry-run.log
    logger.info("loading decisions...")
    decisions = load_latest_decisions(log_file)
    actions = Counter(d["action"] for d in decisions.values())
    logger.info("loaded %d unique thread decisions: %s",
                len(decisions), dict(actions))

    # 2. Load resume state
    applied_ids: set[str] = set()
    if state_file.exists():
        try:
            applied_ids = set(json.loads(state_file.read_text()).get("applied", []))
            logger.info("resume: %d already applied per %s",
                        len(applied_ids), state_file.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not read %s; starting fresh: %s", state_file, e)

    # 3. Compute pending work (only trash + keep; errors get skipped)
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

    # 4. Auth + label resolution
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
            # In dry-run, pretend the labels will exist post-create so the
            # would-be-applied counts match what an actual apply would do.
            for name in missing:
                label_ids[name] = f"<would-create:{name}>"
            logger.info("[dry-run] would create %d new label(s): %s",
                        len(missing), missing)

    # 5. Open audit log
    audit_fh = audit_log.open("a")
    audit_fh.write(
        f"\n=== {_dt.datetime.now(_dt.timezone.utc).isoformat()} "
        f"apply-from-log starting (apply={apply}, limit={limit}) ===\n")
    audit_fh.flush()

    # 6. Batch execution
    counters: Counter[str] = Counter()
    start = time.time()
    decisions_by_id = {d["id"]: d for d in pending}

    def make_callback():
        """Build a callback that has access to counters/log_fh."""
        def cb(request_id, response, exception):
            d = decisions_by_id.get(request_id)
            label = d.get("label") if d else None
            if exception is not None:
                counters["error"] += 1
                rec = {"id": request_id, "result": "error",
                       "err": str(exception)[:200]}
                audit_fh.write(json.dumps(rec) + "\n")
                return
            action = d["action"] if d else "?"
            if action == "trash":
                counters["trash"] += 1
                result = "trash"
            elif action == "keep" and label:
                counters["keep_labeled"] += 1
                result = "keep_labeled"
            else:
                counters["keep_nolabel"] += 1
                result = "keep_nolabel"
            audit_fh.write(json.dumps(
                {"id": request_id, "result": result, "label": label}) + "\n")
        return cb

    callback = make_callback()  # used only by old dry-run path below; real
                                  # apply uses _execute_with_retry directly
    _ = callback  # silence unused-warning in case linter sees it

    # Split pending into batches of BATCH_SIZE
    chunks = [pending[i:i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    total = len(pending)

    for batch_idx, chunk in enumerate(chunks, 1):
        batch = service.new_batch_http_request(callback=callback)
        # Items with no Gmail mutation (keep + no label) get short-circuited;
        # we don't include them in the HTTP batch but still log them.
        http_items_in_batch = 0
        for d in chunk:
            tid = d["id"]
            action = d["action"]
            label = d.get("label")
            if action == "keep" and not label:
                # No Gmail change. Log directly and skip API call.
                counters["keep_nolabel"] += 1
                audit_fh.write(json.dumps(
                    {"id": tid, "result": "keep_nolabel", "label": None}) + "\n")
                if apply:
                    applied_ids.add(tid)
                continue
            if not apply:
                # Dry-run: don't actually call Gmail. Simulate success.
                if action == "trash":
                    counters["trash"] += 1
                    audit_fh.write(json.dumps(
                        {"id": tid, "result": "trash", "label": None}) + "\n")
                else:  # keep with label
                    counters["keep_labeled"] += 1
                    audit_fh.write(json.dumps(
                        {"id": tid, "result": "keep_labeled", "label": label}) + "\n")
                continue
            # Real apply: queue into batch
            if action == "trash":
                req = service.users().threads().trash(userId="me", id=tid)
            else:  # keep + label
                lid = label_ids.get(label)
                if not lid:
                    counters["error"] += 1
                    audit_fh.write(json.dumps(
                        {"id": tid, "result": "error",
                         "err": f"label {label!r} not resolved"}) + "\n")
                    continue
                req = service.users().threads().modify(
                    userId="me", id=tid, body={"addLabelIds": [lid]})
            batch.add(req, request_id=tid)
            http_items_in_batch += 1

        if http_items_in_batch > 0:
            # Items still needing an HTTP call after we filtered out
            # keep_nolabel and dry-run-only items.
            to_apply = [
                d for d in chunk
                if d["action"] == "trash"
                or (d["action"] == "keep" and d.get("label") and label_ids.get(d["label"]))
            ]
            successful = _execute_with_retry(
                service, to_apply, label_ids, audit_fh, counters,
                decisions_by_id, batch_idx)
            if apply:
                applied_ids.update(successful)

        # Per-batch checkpoint
        audit_fh.flush()
        if apply:
            state_file.write_text(json.dumps({"applied": sorted(applied_ids)}))

        done = batch_idx * BATCH_SIZE
        if done > total:
            done = total
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

    audit_fh.flush()
    audit_fh.close()
    if apply:
        state_file.write_text(json.dumps({"applied": sorted(applied_ids)}))

    elapsed = time.time() - start
    logger.info("DONE in %.1f min (%.1f ops/sec). mode=%s counters=%s",
                elapsed / 60, total / elapsed if elapsed else 0,
                mode, dict(counters))


if __name__ == "__main__":
    main()
