#!/usr/bin/env python3
"""Apply dry-run.log decisions to Gmail without re-classifying.

Reads dry-run.log, keeps the latest decision per thread ID, and replays
each decision via the Gmail API:

  - action="trash"           -> users().threads().trash(...)
  - action="keep", label=X   -> users().threads().modify(addLabelIds=[X])
  - action="keep", label=None -> no-op (already kept; nothing to change)
  - action="error"           -> skipped (never applied)

Resumable: applied IDs are checkpointed to state-applied.json after every
--checkpoint-every actions. Re-runs skip already-applied IDs.

Defaults to a dry-run preview. Pass --apply to actually mutate Gmail.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
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


@click.command(context_settings={"max_content_width": 100})
@click.option("--log-file", type=click.Path(exists=True, path_type=Path),
              default=REPO_ROOT / "dry-run.log", show_default=True,
              help="Source of decisions to replay.")
@click.option("--state-file", type=click.Path(path_type=Path),
              default=REPO_ROOT / "state-applied.json", show_default=True,
              help="Tracks applied IDs across runs for resume.")
@click.option("--applied-log", type=click.Path(path_type=Path),
              default=REPO_ROOT / "applied.log", show_default=True,
              help="Audit log of what this script did.")
@click.option("--apply/--dry-run", default=False, show_default=True,
              help="--apply actually mutates Gmail. Default is dry-run preview.")
@click.option("--limit", type=int, default=None,
              help="Apply at most this many actions (for staged rollout).")
@click.option("--concurrency", type=int, default=8, show_default=True,
              help="Parallel Gmail API calls. Gmail's per-user limit is "
                   "~50 ops/sec; default 8 keeps us comfortably under that.")
@click.option("--checkpoint-every", type=int, default=500, show_default=True,
              help="Flush state file + audit log every N actions.")
@click.option("--credentials", type=click.Path(exists=True, path_type=Path),
              default=REPO_ROOT / "config" / "credentials.json", show_default=True)
@click.option("--token", type=click.Path(path_type=Path),
              default=REPO_ROOT / "config" / "token.json", show_default=True)
def main(log_file, state_file, applied_log, apply, limit, concurrency,
         checkpoint_every, credentials, token):
    mode = "APPLY (mutating Gmail)" if apply else "dry-run (no mutations)"
    logger.info("mode: %s", mode)
    logger.info("log file: %s", log_file)

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
        except Exception as e:
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
    label_ids = client.list_labels()

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
            logger.info("[dry-run] would create %d new label(s): %s",
                        len(missing), missing)

    # 5. Open audit log
    applied_log_fh = applied_log.open("a")
    applied_log_fh.write(
        f"\n=== {_dt.datetime.now(_dt.timezone.utc).isoformat()} "
        f"apply-from-log starting (apply={apply}, limit={limit}) ===\n")
    applied_log_fh.flush()

    # 6. Worker
    def do_one(d: dict) -> tuple[str, str, str | None, str | None]:
        tid = d["id"]
        action = d["action"]
        label = d.get("label")
        try:
            if action == "trash":
                if apply:
                    client.trash_thread(tid)
                return tid, "trash", None, None
            # keep
            if label and label in label_ids:
                if apply:
                    client.add_label_to_thread(tid, label_ids[label])
                return tid, "keep_labeled", label, None
            return tid, "keep_nolabel", label, None
        except Exception as e:  # noqa: BLE001
            return tid, "error", None, str(e)

    # 7. Execute
    counters: Counter[str] = Counter()
    start = time.time()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(do_one, d): d for d in pending}
        for i, fut in enumerate(as_completed(futures), 1):
            tid, result, label, err = fut.result()
            counters[result] += 1
            rec = {"id": tid, "result": result, "label": label}
            if err:
                rec["err"] = err[:200]
                logger.warning("error on %s: %s", tid, err)
            applied_log_fh.write(json.dumps(rec) + "\n")

            if apply and result != "error":
                applied_ids.add(tid)

            if i % checkpoint_every == 0:
                applied_log_fh.flush()
                if apply:
                    state_file.write_text(json.dumps({"applied": sorted(applied_ids)}))
                elapsed = time.time() - start
                rate = i / elapsed
                eta_min = int((len(pending) - i) / rate / 60) if rate else 0
                logger.info(
                    "progress: %d/%d (%.1f%%) | %.1f ops/sec | ETA %d min | %s",
                    i, len(pending), 100 * i / len(pending), rate, eta_min,
                    dict(counters))

    # 8. Final flush
    applied_log_fh.flush()
    applied_log_fh.close()
    if apply:
        state_file.write_text(json.dumps({"applied": sorted(applied_ids)}))

    elapsed = time.time() - start
    logger.info("DONE in %.1f min (%.1f ops/sec). mode=%s counters=%s",
                elapsed / 60, len(pending) / elapsed if elapsed else 0,
                mode, dict(counters))


if __name__ == "__main__":
    main()
