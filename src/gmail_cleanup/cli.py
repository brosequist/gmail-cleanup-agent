"""Command-line interface.

Subcommands:
  auth       — run the Google OAuth flow once
  classify   — paginate threads, classify in batches, log decisions, optionally apply
  relabel    — re-label already-kept emails against the current catalog;
               never decides keep-vs-trash, so it cannot trash anything
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import click

from .gmail_client import GmailClient, ThreadSummary
from .prompt import (
    LabelCatalog,
    build_prompt,
    build_relabel_prompt,
    is_whitelisted,
    load_whitelist,
    parse_decisions,
    parse_relabel_decisions,
    validate_decisions_strict,
    validate_relabel_decisions,
)
from .backends import get_backend


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

logger = logging.getLogger("gmail_cleanup")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _client() -> GmailClient:
    return GmailClient(
        credentials_path=CONFIG_DIR / "credentials.json",
        token_path=CONFIG_DIR / "token.json",
    )


# ---------------- subcommands ----------------


@click.group()
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def cli(ctx, verbose):
    setup_logging(verbose)


@cli.command()
def auth():
    """Run the Google OAuth flow. Opens a browser. One-time setup."""
    c = _client()
    c.authorize(force=True)
    click.echo("OK — token saved to config/token.json")


@cli.command()
@click.option(
    "--query",
    default="older_than:90d -has:userlabels -in:trash -in:spam",
    show_default=True,
    help="Gmail search query for threads to process.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Max threads to process (useful for dry-run sampling).",
)
@click.option(
    "--batch-size",
    type=int,
    default=20,
    show_default=True,
    help="Emails per LLM call. Smaller = more reliable JSON output; larger = fewer LLM calls.",
)
@click.option(
    "--llm-retries",
    type=int,
    default=2,
    show_default=True,
    help="Max retries on missing/invalid decisions within a batch. After this, missing emails default to keep-no-label.",
)
@click.option(
    "--apply/--dry-run",
    default=False,
    help="--apply actually trashes/labels in Gmail. --dry-run (default) only logs decisions.",
)
@click.option(
    "--confirm-every",
    type=int,
    default=500,
    show_default=True,
    help="In --apply mode, prompt for y/n every N actions.",
)
@click.option(
    "--state-file",
    type=click.Path(path_type=Path),
    default=REPO_ROOT / "state.json",
    show_default=True,
)
@click.option(
    "--log-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Override default log file.",
)
@click.option(
    "--concurrency",
    type=int,
    default=4,
    show_default=True,
    help="Number of batches to classify in parallel. Set to 1 to disable.",
)
@click.option(
    "--retry-errors/--no-retry-errors",
    default=False,
    show_default=True,
    help="Re-classify threads whose most recent action in the log file is "
         "\"error\". Reads --log-file, finds IDs to retry, and removes them "
         "from the resume-skip set before processing. Use after a transient "
         "backend failure mass-errored a batch.",
)
def classify(query, limit, batch_size, llm_retries, apply, confirm_every,
             state_file, log_file, concurrency, retry_errors):
    """Classify and (optionally) act on threads matching `query`."""

    if log_file is None:
        log_file = REPO_ROOT / ("applied.log" if apply else "dry-run.log")

    catalog = LabelCatalog.load(CONFIG_DIR / "labels.yaml")
    whitelist = load_whitelist(CONFIG_DIR / "whitelist.txt")
    rules = (CONFIG_DIR / "rules.md").read_text()
    backend = get_backend()
    logger.info("backend: %s", backend)
    logger.info("catalog: %d existing + %d auto-create labels",
                len(catalog.existing), len(catalog.auto_create))
    logger.info("whitelist: %d entries", len(whitelist))
    logger.info("query: %s", query)
    logger.info("mode: %s", "APPLY (will modify Gmail)" if apply else "dry-run")

    client = _client()
    client.authorize()

    # Resolve labels in Gmail. For --apply we create missing ones up front;
    # for --dry-run we only verify the existing-name list matches.
    label_ids: dict[str, str] = client.list_labels()
    if apply:
        for name in catalog.auto_create:
            if name not in label_ids:
                logger.info("creating label %r", name)
                label_ids[name] = client.create_label(name)
    else:
        missing = [n for n in catalog.auto_create if n not in label_ids]
        if missing:
            logger.info("--dry-run: would create labels: %s", ", ".join(missing))

    # Resume state — track processed thread IDs to skip on resume
    processed: set[str] = set()
    if state_file.exists():
        try:
            sd = json.loads(state_file.read_text())
            processed = set(sd.get("processed", []))
            logger.info("resuming: %d threads already processed", len(processed))
        except Exception:
            logger.warning("could not read state file %s; starting fresh", state_file)

    # --retry-errors: re-classify threads whose most recent decision in the
    # log file was action=="error". The log is append-mode JSONL across
    # runs, so an ID may have multiple records — only the LATEST matters
    # (a successful re-classification after the error should NOT trigger
    # another retry).
    if retry_errors and log_file.exists():
        latest_action: dict[str, str] = {}
        parsed_records = 0
        with log_file.open() as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("==="):
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                rid = rec.get("id")
                act = rec.get("action")
                if rid and act:
                    latest_action[rid] = act
                    parsed_records += 1
        retry_ids = {rid for rid, act in latest_action.items() if act == "error"}
        before = len(processed)
        processed -= retry_ids
        logger.info(
            "--retry-errors: parsed %d log records, %d threads marked for retry; "
            "resume set %d -> %d",
            parsed_records, len(retry_ids), before, len(processed),
        )
    elif retry_errors:
        logger.info("--retry-errors: log file %s does not exist; no retries to schedule", log_file)

    log_fh = log_file.open("a")
    log_fh.write(f"\n=== {datetime.now(timezone.utc).isoformat()} starting (apply={apply}) ===\n")
    log_fh.flush()

    counters = {"keep": 0, "trash": 0, "whitelist": 0, "errors": 0, "skipped_resume": 0}
    counters_lock = threading.Lock()
    log_lock = threading.Lock()  # serialize writes to log file across workers
    actions_since_confirm = 0

    def maybe_confirm():
        nonlocal actions_since_confirm
        if not apply or confirm_every <= 0:
            return
        if actions_since_confirm < confirm_every:
            return
        click.echo(
            f"\n>>> {actions_since_confirm} actions taken since last confirm. "
            f"Counters so far: {counters}. Continue?",
        )
        if not click.confirm("continue?", default=True):
            click.echo("aborting at user request")
            sys.exit(0)
        actions_since_confirm = 0

    def report_progress():
        """Cumulative count + per-action share. Called after every batch completes."""
        reviewed = counters["keep"] + counters["trash"] + counters["whitelist"] + counters["errors"]
        if reviewed == 0:
            return
        target_clause = f" / {limit} target ({reviewed/limit*100:.1f}%)" if limit else ""
        parts = [f"reviewed: {reviewed}{target_clause}"]
        for k in ("keep", "trash", "whitelist", "errors"):
            v = counters[k]
            pct = v / reviewed * 100 if reviewed else 0
            parts.append(f"{k}: {v} ({pct:.1f}%)")
        logger.info("progress | " + " | ".join(parts))

    try:
        batch: list[ThreadSummary] = []
        # Buffer of batches awaiting concurrent submission. We submit `concurrency`
        # batches at a time, wait for all to complete, then submit the next group.
        # Simpler than continuous-pipeline + plenty fast for our workload.
        batch_buffer: list[list[ThreadSummary]] = []

        def flush_buffer():
            """Classify all batches in batch_buffer concurrently, then apply
            Gmail mutations (sequentially in main thread since the Gmail
            client isn't thread-safe). Updates state + log + counters."""
            nonlocal actions_since_confirm
            if not batch_buffer:
                return
            results: dict[int, tuple[list[ThreadSummary], list[dict], list[ThreadSummary]]] = {}
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futures = {
                    ex.submit(_classify_pure, b, rules, catalog, whitelist,
                              backend, llm_retries): i
                    for i, b in enumerate(batch_buffer)
                }
                for fut in as_completed(futures):
                    idx = futures[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception as e:
                        # Shouldn't happen — _classify_pure handles its own errors —
                        # but if it does, mark the whole batch as errored.
                        b = batch_buffer[idx]
                        logger.error("classifier worker died: %s", e)
                        results[idx] = (b, [], [])
            # Apply sequentially in submission order so log + state ordering is stable
            for i, b in enumerate(batch_buffer):
                threads, decisions, whitelisted = results[i]
                _apply_decisions(threads, decisions, whitelisted, label_ids,
                                 client, apply, log_fh, log_lock,
                                 counters, counters_lock)
                processed.update(t.thread_id for t in b)
                actions_since_confirm += len(b)
            _checkpoint(state_file, processed)
            report_progress()
            batch_buffer.clear()
            maybe_confirm()

        for t in client.search_threads(query, max_threads=limit, skip_ids=processed):
            if t.thread_id in processed:
                counters["skipped_resume"] += 1
                continue
            batch.append(t)
            if len(batch) >= batch_size:
                batch_buffer.append(batch)
                batch = []
                if len(batch_buffer) >= concurrency:
                    flush_buffer()
        if batch:
            batch_buffer.append(batch)
        flush_buffer()
    finally:
        log_fh.write(f"=== done. counters: {json.dumps(counters)} ===\n")
        log_fh.flush()
        log_fh.close()
        logger.info("counters: %s", counters)
        logger.info("log: %s", log_file)
        logger.info("state: %s", state_file)


@cli.command()
@click.option(
    "--input-log",
    type=click.Path(path_type=Path, exists=True),
    default=REPO_ROOT / "dry-run.log",
    show_default=True,
    help="Decision log to read already-kept emails from (dry-run.log or applied.log).",
)
@click.option(
    "--batch-size", type=int, default=20, show_default=True,
    help="Emails per LLM call.",
)
@click.option(
    "--llm-retries", type=int, default=2, show_default=True,
    help="Max retries on missing/invalid labels within a batch. After this, the email keeps its existing label.",
)
@click.option(
    "--apply/--dry-run", default=False,
    help="--apply actually moves labels in Gmail. --dry-run (default) only logs proposed changes.",
)
@click.option(
    "--confirm-every", type=int, default=500, show_default=True,
    help="In --apply mode, prompt for y/n every N label changes.",
)
@click.option(
    "--concurrency", type=int, default=4, show_default=True,
    help="Number of batches to relabel in parallel.",
)
@click.option(
    "--refetch-snippets", is_flag=True,
    help="Fetch each email's snippet from Gmail for richer context (1 API call per email — slower, but better labels).",
)
@click.option(
    "--state-file", type=click.Path(path_type=Path),
    default=REPO_ROOT / "relabel-state.json", show_default=True,
)
@click.option(
    "--log-file", type=click.Path(path_type=Path), default=None,
    help="Override default log file (relabel.log).",
)
def relabel(input_log, batch_size, llm_retries, apply, confirm_every,
            concurrency, refetch_snippets, state_file, log_file):
    """Re-label already-kept emails against the current label catalog.

    Reads `keep` decisions from a prior decision log, re-asks the LLM to
    pick the best label for each from the *current* config/labels.yaml,
    and (with --apply) moves the Gmail label. It NEVER decides
    keep-vs-trash and never calls trash — emails that were kept stay
    kept. Use this after expanding your label catalog to reorganize
    without redoing the trash decisions.
    """
    if log_file is None:
        log_file = REPO_ROOT / "relabel.log"

    catalog = LabelCatalog.load(CONFIG_DIR / "labels.yaml")
    backend = get_backend()
    logger.info("backend: %s", backend)
    logger.info("catalog: %d keep-labels available", len(catalog.all_keep_labels))
    logger.info("input log: %s", input_log)
    logger.info("mode: %s", "APPLY (will move Gmail labels)" if apply else "dry-run")

    # Read already-kept emails from the input log. Keep the LAST decision
    # per id (in case the log has re-run duplicates). Skip trash / error
    # rows entirely — relabel only touches kept mail.
    kept: dict[str, dict] = {}
    with open(input_log) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("==="):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("action") != "keep":
                continue
            kept[rec["id"]] = rec
    logger.info("found %d already-kept emails in %s", len(kept), input_log)
    if not kept:
        logger.info("nothing to relabel")
        return

    client = _client()
    client.authorize()
    label_ids: dict[str, str] = client.list_labels()
    if apply:
        for name in catalog.auto_create:
            if name not in label_ids:
                logger.info("creating label %r", name)
                label_ids[name] = client.create_label(name)
    else:
        missing = [n for n in catalog.auto_create if n not in label_ids]
        if missing:
            logger.info("--dry-run: would create labels: %s", ", ".join(missing))

    # Resume — relabel uses its own state file so it never collides with
    # the classify checkpoint.
    processed: set[str] = set()
    if state_file.exists():
        try:
            processed = set(json.loads(state_file.read_text()).get("processed", []))
            logger.info("resuming: %d emails already relabeled", len(processed))
        except Exception:
            logger.warning("could not read %s; starting fresh", state_file)

    work = [rec for rid, rec in kept.items() if rid not in processed]
    logger.info("%d emails to relabel (%d skipped from resume state)",
                len(work), len(kept) - len(work))

    log_fh = log_file.open("a")
    log_fh.write(f"\n=== {datetime.now(timezone.utc).isoformat()} relabel starting (apply={apply}) ===\n")
    log_fh.flush()

    counters = {"changed": 0, "unchanged": 0, "errors": 0, "skipped_resume": len(kept) - len(work)}
    counters_lock = threading.Lock()
    log_lock = threading.Lock()
    actions_since_confirm = 0

    def maybe_confirm():
        nonlocal actions_since_confirm
        if not apply or confirm_every <= 0 or actions_since_confirm < confirm_every:
            return
        click.echo(f"\n>>> {actions_since_confirm} label changes applied. Counters: {counters}.")
        if not click.confirm("continue?", default=True):
            click.echo("aborting at user request")
            sys.exit(0)
        actions_since_confirm = 0

    def report_progress():
        done = counters["changed"] + counters["unchanged"] + counters["errors"]
        if done:
            logger.info("progress | relabeled: %d | changed: %d | unchanged: %d | errors: %d",
                        done, counters["changed"], counters["unchanged"], counters["errors"])

    try:
        buffer: list[list[dict]] = []
        cur: list[dict] = []

        def flush():
            nonlocal actions_since_confirm
            if not buffer:
                return
            results: dict[int, list[dict]] = {}
            with ThreadPoolExecutor(max_workers=concurrency) as ex:
                futures = {
                    ex.submit(_relabel_pure, b, catalog, backend, llm_retries,
                              client if refetch_snippets else None): i
                    for i, b in enumerate(buffer)
                }
                for fut in as_completed(futures):
                    idx = futures[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception as e:
                        logger.error("relabel worker died: %s", e)
                        results[idx] = [
                            {"id": r["id"], "label": r.get("label"), "_err": str(e)}
                            for r in buffer[idx]
                        ]
            for i, b in enumerate(buffer):
                by_id = {r["id"]: r for r in b}
                for d in results[i]:
                    rec = by_id.get(d["id"])
                    if rec is None:
                        continue
                    old_label = rec.get("label")
                    new_label = d.get("label")
                    err = d.get("_err")
                    if err:
                        with counters_lock:
                            counters["errors"] += 1
                        with log_lock:
                            _log_relabel(log_fh, rec, old_label, old_label, False,
                                         f"error: {err[:200]}")
                        continue
                    changed = new_label != old_label
                    if changed and apply:
                        add_ids = [label_ids[new_label]] if new_label in label_ids else []
                        remove_ids = [label_ids[old_label]] if old_label in label_ids else []
                        if add_ids:
                            try:
                                client.modify_thread_labels(
                                    rec["id"], add_label_ids=add_ids,
                                    remove_label_ids=remove_ids)
                            except Exception as e:
                                logger.error("relabel apply failed for %s: %s", rec["id"], e)
                                with counters_lock:
                                    counters["errors"] += 1
                                with log_lock:
                                    _log_relabel(log_fh, rec, old_label, new_label, False,
                                                 f"apply failed: {e}")
                                continue
                        else:
                            logger.warning("new label %r has no Gmail id; logging only", new_label)
                    with counters_lock:
                        counters["changed" if changed else "unchanged"] += 1
                    with log_lock:
                        _log_relabel(log_fh, rec, old_label, new_label, changed,
                                     "applied" if (changed and apply) else "")
                    processed.add(rec["id"])
                    if changed and apply:
                        actions_since_confirm += 1
            _checkpoint(state_file, processed)
            report_progress()
            buffer.clear()
            maybe_confirm()

        for rec in work:
            cur.append(rec)
            if len(cur) >= batch_size:
                buffer.append(cur)
                cur = []
                if len(buffer) >= concurrency:
                    flush()
        if cur:
            buffer.append(cur)
        flush()
    finally:
        log_fh.write(f"=== done. counters: {json.dumps(counters)} ===\n")
        log_fh.flush()
        log_fh.close()
        logger.info("counters: %s", counters)
        logger.info("log: %s", log_file)
        logger.info("state: %s", state_file)


def _relabel_pure(
    batch: list[dict], catalog: LabelCatalog, backend, llm_retries: int,
    client=None,
) -> list[dict]:
    """Pure relabel worker, safe to call from a thread. `batch` items are
    decision-log records ({id, from, subject, label}). If `client` is
    given, snippets are re-fetched from Gmail for richer context.
    Returns a list of {id, label} — label is the model's choice, or the
    email's existing label if the model never produced a valid one.
    Never decides keep-vs-trash."""
    llm_batch: list[dict] = []
    for rec in batch:
        item = {
            "id": rec["id"],
            "sender": rec.get("from", ""),
            "subject": rec.get("subject", ""),
            "current_label": rec.get("label"),
        }
        if client is not None:
            try:
                meta = client.fetch_thread_meta(rec["id"])
                if meta is not None:
                    item["snippet"] = meta.snippet
                    # prefer freshly-fetched sender/subject if the log truncated them
                    item["sender"] = meta.sender or item["sender"]
                    item["subject"] = meta.subject or item["subject"]
            except Exception as e:
                logger.warning("snippet refetch failed for %s: %s", rec["id"], e)
        llm_batch.append(item)

    decisions: list[dict] = []
    remaining = list(llm_batch)
    for attempt in range(llm_retries + 1):
        if not remaining:
            break
        prompt = build_relabel_prompt(catalog, remaining)
        try:
            t0 = time.monotonic()
            raw = backend.classify_batch(prompt)
            dur = time.monotonic() - t0
            tag = "" if attempt == 0 else f" (retry {attempt})"
            logger.info("relabeled %d emails in %.1fs%s", len(remaining), dur, tag)
        except Exception as e:
            logger.error("backend error: %s", e)
            if attempt == 0:
                # First call failed entirely — surface every email as an error
                return [{"id": x["id"], "label": x.get("current_label"), "_err": str(e)}
                        for x in llm_batch]
            break

        parsed = parse_relabel_decisions(raw)
        good, missing_ids, errs = validate_relabel_decisions(parsed, remaining, catalog)
        for ev in errs:
            logger.warning("relabel validate: %s", ev)
        decisions.extend(good)
        if missing_ids and attempt < llm_retries:
            logger.warning("attempt %d: %d emails missing labels, retrying just those",
                           attempt + 1, len(missing_ids))
        remaining = [x for x in remaining if x["id"] in missing_ids]

    # Anything still unlabeled by the model keeps its existing label —
    # never drop a label just because the model fumbled the JSON.
    for x in remaining:
        logger.warning("id=%s: no valid label after %d retries; keeping existing label %r",
                       x["id"], llm_retries, x.get("current_label"))
        decisions.append({"id": x["id"], "label": x.get("current_label")})

    return decisions


def _log_relabel(fh, rec: dict, old_label, new_label, changed: bool, note: str):
    out = {
        "id": rec["id"],
        "from": rec.get("from", ""),
        "subject": (rec.get("subject", "") or "")[:120],
        "old_label": old_label,
        "new_label": new_label,
        "changed": changed,
        "note": note,
    }
    fh.write(json.dumps(out, ensure_ascii=False) + "\n")
    fh.flush()


def _classify_pure(
    batch: list[ThreadSummary], rules: str, catalog: LabelCatalog,
    whitelist: list, backend, llm_retries: int = 2,
) -> tuple[list[ThreadSummary], list[dict], list[ThreadSummary]]:
    """Pure classifier function safe to call from a worker thread.

    Splits the batch into whitelisted and LLM-bound, runs the LLM call
    with retry-on-missing (handles the LLM occasionally dropping/repeating
    items in batched JSON output), and returns:
      (original_threads, decisions_for_llm_threads, whitelisted_threads)

    The retry strategy: if any input ids didn't get a valid decision in
    the first call's response, re-prompt with just those ids. Up to
    `llm_retries` retries. Anything still missing afterwards gets
    keep-no-label (safe failure mode — preserves the email).

    Does NOT touch Gmail or counters. The caller (main thread) handles
    those after this returns.
    """
    llm_batch: list[dict] = []
    whitelisted: list[ThreadSummary] = []
    for t in batch:
        if is_whitelisted(t.sender, whitelist):
            whitelisted.append(t)
        else:
            llm_batch.append({
                "id": t.thread_id, "sender": t.sender,
                "subject": t.subject, "snippet": t.snippet,
                "age_days": t.age_days,
                "has_list_unsubscribe": t.has_list_unsubscribe,
            })

    decisions: list[dict] = []
    remaining = list(llm_batch)
    for attempt in range(llm_retries + 1):
        if not remaining:
            break
        prompt = build_prompt(rules, catalog, remaining)
        try:
            t0 = time.monotonic()
            raw = backend.classify_batch(prompt)
            dur = time.monotonic() - t0
            tag = "" if attempt == 0 else f" (retry {attempt})"
            logger.info("classified %d emails in %.1fs%s", len(remaining), dur, tag)
        except Exception as e:
            logger.error("backend error: %s", e)
            if attempt == 0:
                # First call failed entirely — surface as full-batch error
                for x in llm_batch:
                    decisions.append({"id": x["id"], "action": "error",
                                      "label": None, "_err": str(e)})
                return batch, decisions, whitelisted
            # Retry call failed — break out and accept what we have
            break

        parsed = parse_decisions(raw)
        good, missing_ids, errs = validate_decisions_strict(parsed, remaining, catalog)
        # Only log validate errors that aren't just the "missing" pattern
        # (we handle missing explicitly via retry below).
        for ev in errs:
            logger.warning("validate: %s", ev)
        decisions.extend(good)

        if missing_ids and attempt < llm_retries:
            logger.warning("attempt %d: %d emails missing decisions, retrying just those",
                           attempt + 1, len(missing_ids))
        remaining = [x for x in remaining if x["id"] in missing_ids]

    # Anything still remaining after all retries → safe default (keep-no-label)
    for x in remaining:
        logger.warning("id=%s: still missing after %d retries; defaulting to keep",
                       x["id"], llm_retries)
        decisions.append({"id": x["id"], "action": "keep", "label": None})

    return batch, decisions, whitelisted


def _apply_decisions(
    batch: list[ThreadSummary], decisions: list[dict],
    whitelisted: list[ThreadSummary], label_ids: dict[str, str], client,
    apply: bool, log_fh, log_lock, counters: dict, counters_lock,
) -> None:
    """Apply (or just log) decisions for one batch. Runs on the main
    thread — Gmail client isn't thread-safe and we want stable log
    ordering."""
    # Whitelisted threads — emit KEEP-no-label decision; no Gmail mutation.
    for t in whitelisted:
        with counters_lock:
            counters["whitelist"] += 1
        with log_lock:
            _log(log_fh, t, "keep", None, "whitelist")

    by_id = {t.thread_id: t for t in batch}
    for d in decisions:
        t = by_id.get(d["id"])
        if not t:
            continue
        action = d.get("action")
        if action == "error":
            with counters_lock:
                counters["errors"] += 1
            with log_lock:
                _log(log_fh, t, "error", None, f"backend: {d.get('_err','')[:200]}")
            continue
        if action == "trash":
            with counters_lock:
                counters["trash"] += 1
            with log_lock:
                _log(log_fh, t, "trash", None, "" if not apply else "applied")
            if apply:
                try:
                    client.trash_thread(t.thread_id)
                except Exception as e:
                    logger.error("trash failed for %s: %s", t.thread_id, e)
                    with counters_lock:
                        counters["errors"] += 1
        else:  # keep
            label = d.get("label")
            with counters_lock:
                counters["keep"] += 1
            with log_lock:
                _log(log_fh, t, "keep", label, "" if not apply else "applied")
            if apply and label:
                lid = label_ids.get(label)
                if lid:
                    try:
                        client.add_label_to_thread(t.thread_id, lid)
                    except Exception as e:
                        logger.error("label failed for %s: %s", t.thread_id, e)
                        with counters_lock:
                            counters["errors"] += 1


def _log(fh, t: ThreadSummary, action: str, label: str | None, note: str):
    rec = {
        "id": t.thread_id,
        "from": t.sender,
        "subject": t.subject[:120],
        "action": action,
        "label": label,
        "note": note,
    }
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fh.flush()


def _checkpoint(state_file: Path, processed: set[str]) -> None:
    state_file.write_text(json.dumps({"processed": sorted(processed)}))


def main():
    cli()


if __name__ == "__main__":
    main()
