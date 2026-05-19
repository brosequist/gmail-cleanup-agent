#!/usr/bin/env python3
"""Post-apply cleanup: trash 'keep' decisions that match known junk patterns.

The first classification pass left ~3.4% of keeps over-cautious. This script
flips those specific patterns to trash WITHOUT re-running the LLM — pure
sender/subject regex matching against decisions in dry-run.log.

Categories (all default ON):

  signin   — old sign-in / verification-code / account-access notices
             (useful when fresh, noise when years old)
  digest   — recurring daily digests (Vet Tix etc.) that reference
             registrations-worthy events but have no archival value
  linkedin — LinkedIn job alerts (recurring "[role]: Company")
  social   — old reply notifications from Nextdoor / Reddit / Quora /
             Facebook (snippet often quotes the original post and looks
             personal, but the notification itself is automated)

Defaults to dry-run preview. --apply mutates Gmail. Resumable via
state-junk-cleanup.json. Same batch HTTP + retry-on-429 pattern as
apply_from_log.py.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
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
logger = logging.getLogger("cleanup_junk_keeps")

BATCH_SIZE = 20
MAX_429_RETRIES = 5


# --- category predicates ---

_SIGNIN_RE = re.compile(
    r"\b(sign[- ]?in|new sign|verification|verify|access code|access alert|account alert)\b",
    re.I,
)
_DIGEST_DOMS = {"vettix.org", "vettixer.org"}
_SOCIAL_DOMS = {
    "rs.email.nextdoor.com", "redditmail.com", "quora.com",
    "m.facebook.com", "notifications.facebook.com",
}


def _domain(addr: str | None) -> str:
    m = re.search(r"@([\w.\-]+)", addr or "")
    return m.group(1).lower() if m else ""


def category_for(record: dict) -> str | None:
    """Return the category label this record matches, or None."""
    subject = record.get("subject") or ""
    from_addr = record.get("from") or ""
    d = _domain(from_addr)
    if _SIGNIN_RE.search(subject):
        return "signin"
    if d in _DIGEST_DOMS:
        return "digest"
    if d == "linkedin.com":
        return "linkedin"
    if d in _SOCIAL_DOMS:
        return "social"
    return None


def load_latest_decisions(log_path: Path) -> dict[str, dict]:
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


def _execute_with_retry(service, items, audit_fh, counters, batch_idx):
    """Batch HTTP execute with 429 retry. Returns set of IDs successfully trashed."""
    successful: set[str] = set()
    pending = list(items)
    for attempt in range(MAX_429_RETRIES + 1):
        if not pending:
            break
        retry: list[dict] = []

        def cb(request_id, response, exception, _retry=retry, _items=pending):
            if exception is None:
                successful.add(request_id)
                rec = next((it for it in _items if it["id"] == request_id), {})
                audit_fh.write(json.dumps({
                    "id": request_id, "result": "trash",
                    "category": rec.get("_category"),
                    "from": rec.get("from", "")[:80],
                    "subject": (rec.get("subject") or "")[:120],
                }) + "\n")
                counters["trash"] += 1
                return
            err = str(exception)
            if "429" in err or "rate" in err.lower() or "concurrent" in err.lower():
                rec = next((it for it in _items if it["id"] == request_id), None)
                if rec is not None:
                    _retry.append(rec)
            else:
                counters["error"] += 1
                audit_fh.write(json.dumps({
                    "id": request_id, "result": "error", "err": err[:200],
                }) + "\n")

        batch = service.new_batch_http_request(callback=cb)
        for d in pending:
            batch.add(service.users().threads().trash(userId="me", id=d["id"]),
                      request_id=d["id"])
        try:
            batch.execute()
        except Exception as e:  # noqa: BLE001
            logger.error("batch %d transport failed on attempt %d: %s",
                         batch_idx, attempt + 1, e)
            retry = pending[:]

        if retry:
            sleep_for = 1.5 * (2 ** attempt)
            logger.warning("  batch %d: %d items hit 429 attempt %d, backing off %.1fs",
                           batch_idx, len(retry), attempt + 1, sleep_for)
            time.sleep(sleep_for)
        pending = retry

    for d in pending:
        counters["error"] += 1
        audit_fh.write(json.dumps({
            "id": d["id"], "result": "error",
            "err": f"429 after {MAX_429_RETRIES} retries",
        }) + "\n")
    return successful


@click.command(context_settings={"max_content_width": 100})
@click.option("--log-file", type=click.Path(exists=True, path_type=Path),
              default=REPO_ROOT / "dry-run.log", show_default=True)
@click.option("--state-file", type=click.Path(path_type=Path),
              default=REPO_ROOT / "state-junk-cleanup.json", show_default=True)
@click.option("--audit-log", type=click.Path(path_type=Path),
              default=REPO_ROOT / "junk-cleanup.log", show_default=True)
@click.option("--apply/--dry-run", default=False, show_default=True,
              help="--apply actually trashes in Gmail. Default is dry-run preview.")
@click.option("--limit", type=int, default=None,
              help="Cap candidates (for staged rollout).")
@click.option("--categories", default="signin,digest,linkedin,social",
              show_default=True,
              help="Comma-separated subset to act on. Available: "
                   "signin, digest, linkedin, social.")
@click.option("--batch-size", type=int, default=BATCH_SIZE, show_default=True)
@click.option("--batch-sleep", type=float, default=1.5, show_default=True)
@click.option("--credentials", type=click.Path(exists=True, path_type=Path),
              default=REPO_ROOT / "config" / "credentials.json", show_default=True)
@click.option("--token", type=click.Path(path_type=Path),
              default=REPO_ROOT / "config" / "token.json", show_default=True)
def main(log_file, state_file, audit_log, apply, limit, categories,
         batch_size, batch_sleep, credentials, token):
    mode = "APPLY (trashing in Gmail)" if apply else "dry-run (no mutations)"
    logger.info("mode: %s", mode)

    active_cats = {c.strip() for c in categories.split(",") if c.strip()}
    logger.info("active categories: %s", sorted(active_cats))

    decisions = load_latest_decisions(log_file)
    keeps = [r for r in decisions.values() if r["action"] == "keep"]
    logger.info("loaded %d total decisions, %d keeps", len(decisions), len(keeps))

    candidates: list[dict] = []
    cat_counts: Counter[str] = Counter()
    for r in keeps:
        cat = category_for(r)
        if cat and cat in active_cats:
            r2 = dict(r)
            r2["_category"] = cat
            candidates.append(r2)
            cat_counts[cat] += 1

    logger.info("candidate breakdown:")
    for cat in sorted(active_cats):
        logger.info("  %-10s %d", cat, cat_counts.get(cat, 0))
    logger.info("  TOTAL    %d", len(candidates))

    # Resume
    applied: set[str] = set()
    if state_file.exists():
        try:
            applied = set(json.loads(state_file.read_text()).get("applied", []))
            logger.info("resume: %d already trashed per %s",
                        len(applied), state_file.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("could not read %s; starting fresh: %s", state_file, e)

    pending = [c for c in candidates if c["id"] not in applied]
    if limit is not None:
        pending = pending[:limit]
    logger.info("pending: %d", len(pending))

    if not pending:
        logger.info("nothing to do; exiting")
        return

    client = GmailClient(credentials, token)
    client.authorize()
    service = client._service  # noqa: SLF001

    audit_fh = audit_log.open("a")
    audit_fh.write(
        f"\n=== {_dt.datetime.now(_dt.timezone.utc).isoformat()} "
        f"cleanup_junk_keeps starting (apply={apply}, cats={sorted(active_cats)}, "
        f"limit={limit}) ===\n")
    audit_fh.flush()

    counters: Counter[str] = Counter()
    start = time.time()
    chunks = [pending[i:i + batch_size]
              for i in range(0, len(pending), batch_size)]

    for batch_idx, chunk in enumerate(chunks, 1):
        if not apply:
            # dry-run: log what would happen, no API call
            for d in chunk:
                counters["trash"] += 1
                audit_fh.write(json.dumps({
                    "id": d["id"], "result": "would_trash",
                    "category": d["_category"],
                    "from": d.get("from", "")[:80],
                    "subject": (d.get("subject") or "")[:120],
                }) + "\n")
        else:
            successful = _execute_with_retry(
                service, chunk, audit_fh, counters, batch_idx)
            applied.update(successful)

        audit_fh.flush()
        if apply:
            state_file.write_text(json.dumps({"applied": sorted(applied)}))

        done = min(batch_idx * batch_size, len(pending))
        elapsed = time.time() - start
        rate = done / elapsed if elapsed > 0 else 0
        eta_min = int((len(pending) - done) / rate / 60) if rate > 0 else 0
        if batch_idx % 5 == 0 or batch_idx == len(chunks):
            logger.info("batch %d/%d (%d/%d, %.1f%%) | %.1f ops/sec | ETA %d min | %s",
                        batch_idx, len(chunks), done, len(pending),
                        100 * done / len(pending), rate, eta_min, dict(counters))

        if apply and batch_idx < len(chunks):
            time.sleep(batch_sleep)

    audit_fh.flush()
    audit_fh.close()
    if apply:
        state_file.write_text(json.dumps({"applied": sorted(applied)}))

    elapsed = time.time() - start
    logger.info("DONE in %.1f min. mode=%s counters=%s",
                elapsed / 60, mode, dict(counters))


if __name__ == "__main__":
    main()
