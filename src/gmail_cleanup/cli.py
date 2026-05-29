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
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import click

from . import applylog
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


# REPO_ROOT and CONFIG_DIR resolve at runtime, not at module import time,
# so the CLI works the same whether it's run from an editable checkout
# (`pip install -e .`), a wheel install from PyPI, or a Docker image.
#
# Resolution order for the config dir:
#   1. $GMAIL_CLEANUP_CONFIG_DIR if set (explicit override; Docker uses this)
#   2. ./config in the current working directory if that's an existing dir
#   3. <cwd>/config as a writable default (created on first config copy)
#
# State / log defaults are similarly anchored at the current working
# directory so a PyPI-installed `gmail-cleanup` produces files where the
# user invoked it, not in site-packages.
def _work_root() -> Path:
    return Path(os.environ.get("GMAIL_CLEANUP_WORK_DIR", Path.cwd())).resolve()


def _resolve_config_dir() -> Path:
    env = os.environ.get("GMAIL_CLEANUP_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return _work_root() / "config"


# Module-level aliases kept for backward compat with the test suite
# (which monkeypatches these). Treat them as the *current* resolved
# values — re-evaluate inside functions when freshness matters.
REPO_ROOT = _work_root()
CONFIG_DIR = _resolve_config_dir()

logger = logging.getLogger("gmail_cleanup")


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _client() -> GmailClient:
    # Always re-resolve so a freshly-set env var or a test monkeypatch
    # of CONFIG_DIR is picked up at call time.
    cfg = Path(os.environ.get("GMAIL_CLEANUP_CONFIG_DIR")) if os.environ.get(
        "GMAIL_CLEANUP_CONFIG_DIR") else CONFIG_DIR
    return GmailClient(
        credentials_path=cfg / "credentials.json",
        token_path=cfg / "token.json",
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
    default=None,
    help="Resume checkpoint of processed thread IDs. Default: ./state.json.",
)
@click.option(
    "--log-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Decision log. Default: ./dry-run.log (or ./applied.log under --apply).",
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
@click.option(
    "--include-body/--no-include-body",
    default=False,
    show_default=True,
    help="Fetch and include the first message's body (text/plain, fall back to "
         "text/html stripped) in the prompt, capped at 4 KB per email. Off by "
         "default — snippet + headers are enough for bulk triage. Use for "
         "higher-stakes runs where the snippet is ambiguous. Roughly 2-3x the "
         "Gmail API payload size and ~+30% prompt tokens per email.",
)
@click.option(
    "--console-log",
    type=click.Path(path_type=Path),
    default=None,
    help="Mirror logger output to this file in addition to stderr. Useful "
         "for long-running cleanup passes you want to grep / tail later.",
)
@click.option(
    "--reviewed-label",
    metavar="[NAME]",
    is_flag=False,
    flag_value="Reviewed",
    default=None,
    help="Apply a label to every email the LLM keeps — including whitelisted "
         "senders — regardless of which category label it got. Trashed and "
         "errored emails are NOT labeled. The label name is also excluded "
         "from this and future runs, so a re-run never re-reviews mail the "
         "tool already finished. Pass bare (--reviewed-label) for the default "
         "name \"Reviewed\", or --reviewed-label=NAME for a custom name. "
         "Off when omitted.",
)
@click.option(
    "--skip-label",
    "skip_labels",
    metavar="NAME",
    multiple=True,
    help="Exclude emails carrying label NAME from classification (server-side "
         "Gmail query filter — excluded mail is never even listed). "
         "Repeatable: pass it several times to skip several labels. The "
         "--reviewed-label name is skipped automatically; use this for "
         "additional, often manually-applied labels. Off when omitted.",
)
@click.option(
    "--remove-label",
    "remove_labels_forced",
    metavar="NAME",
    multiple=True,
    help="Unconditionally strip label NAME from every thread classify "
         "touches (kept and whitelisted alike). Trashed threads are "
         "skipped — trashing already hides labels. Repeatable. Pair this "
         "with `--query 'label:NAME'` to retire a legacy label across the "
         "mailbox in one pass. For the LLM to *propose* removals "
         "per-thread instead, define a `removable:` section in "
         "config/labels.yaml. Off when omitted.",
)
def classify(query, limit, batch_size, llm_retries, apply, confirm_every,
             state_file, log_file, concurrency, retry_errors, include_body,
             console_log, reviewed_label, skip_labels, remove_labels_forced):
    """Classify and (optionally) act on threads matching `query`."""

    if console_log:
        # Add a FileHandler to the root logger so every logger.* call
        # in the run also lands in the file. Append mode — multiple
        # session resumes accumulate into the same file, matching the
        # log-file behavior elsewhere in this command.
        fh = logging.FileHandler(console_log, mode="a")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
        logger.info("mirroring console output to %s", console_log)

    work = _work_root()
    cfg = _resolve_config_dir()
    if state_file is None:
        state_file = work / "state.json"
    if log_file is None:
        log_file = work / ("applied.log" if apply else "dry-run.log")

    # --reviewed-label / --skip-label — both opt-in, unset by default.
    # The reviewed-label name (default "Reviewed" when the flag is given
    # bare) is always added to the skip set, so a re-run never re-reviews
    # mail this tool already finished. --skip-label adds further labels to
    # exclude — handy for protecting manually-applied "do not touch"
    # labels. Skipping is a server-side Gmail query filter (`-label:"…"`),
    # so excluded mail is never even listed: no metadata fetch, no LLM call.
    #
    # A label name with a `"` in it cannot be expressed as a Gmail query
    # term (the term itself is `"`-delimited and Gmail search has no escape
    # for an inner quote). Such a name is dropped from the skip filter with
    # a warning rather than emitted as a malformed query — the label can
    # still be applied normally; only the skip optimisation is lost.
    requested_skip: list[str] = []
    for name in list(skip_labels) + ([reviewed_label] if reviewed_label else []):
        if name and name not in requested_skip:
            requested_skip.append(name)
    skip_names: list[str] = []
    for name in requested_skip:
        if '"' in name:
            logger.warning(
                "label %r contains a double-quote — Gmail search cannot "
                "express it as a query term, so it will NOT be skip-filtered "
                "(the label is still applied normally if it is the "
                "reviewed-label).", name)
            continue
        skip_names.append(name)
        if f'label:"{name}"' not in query:
            query = f'{query} -label:"{name}"'.strip()

    catalog = LabelCatalog.load(cfg / "labels.yaml")
    whitelist = load_whitelist(cfg / "whitelist.txt")
    rules = (cfg / "rules.md").read_text()
    backend = get_backend()
    logger.info("backend: %s", backend)
    logger.info("catalog: %d existing + %d auto-create labels",
                len(catalog.existing), len(catalog.auto_create))
    logger.info("whitelist: %d entries", len(whitelist))
    logger.info("query: %s", query)
    if reviewed_label:
        logger.info("reviewed-label: %r (applied to kept + whitelisted emails)",
                    reviewed_label)
        if reviewed_label.lower() in {lbl.lower()
                                      for lbl in catalog.all_keep_labels}:
            logger.warning(
                "reviewed-label %r is also a category label in labels.yaml — "
                "every email filed under that category will be excluded from "
                "future classify runs by the skip filter. Use a distinct name "
                "if that is not what you intend.",
                reviewed_label,
            )
    if skip_names:
        logger.info("skip-labels (excluded from classification): %s",
                    ", ".join(skip_names))
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

    # Resolve the reviewed-label against existing Gmail labels
    # case-insensitively: if the account already has a label that differs
    # only in casing (e.g. user typed "reviewed", label "Reviewed" exists),
    # reuse the existing label instead of creating a near-duplicate.
    if reviewed_label and reviewed_label not in label_ids:
        ci_match = next((name for name in label_ids
                         if name.lower() == reviewed_label.lower()), None)
        if ci_match:
            logger.info("reviewed-label %r matches existing Gmail label %r "
                        "(case-insensitive) — using the existing label",
                        reviewed_label, ci_match)
            reviewed_label = ci_match

    # The reviewed-label needs to exist before we can apply it. Skip-only
    # labels are pure query filters and don't need to exist (a `-label:`
    # term for a nonexistent label simply matches nothing).
    if reviewed_label and reviewed_label not in label_ids:
        if apply:
            logger.info("creating reviewed-label %r", reviewed_label)
            label_ids[reviewed_label] = client.create_label(reviewed_label)
        else:
            logger.info("--dry-run: would create reviewed-label %r", reviewed_label)

    # --remove-label: resolve to (name, id) pairs once. A label that
    # doesn't exist in Gmail is dropped with a warning — there's nothing
    # to strip. A label that's also in catalog.removable is allowed but
    # warned: it means the forced strip will always win over LLM
    # judgment, which is probably not what the user wanted if they took
    # the trouble to describe it as removable.
    forced_remove: list[tuple[str, str]] = []
    for name in remove_labels_forced:
        lid = label_ids.get(name)
        if lid is None:
            logger.warning(
                "--remove-label %r is not a Gmail label in this account; "
                "ignoring (nothing to strip).", name)
            continue
        if name in catalog.removable:
            logger.warning(
                "--remove-label %r is also in the labels.yaml `removable:` "
                "catalog. The forced strip will always remove it, making the "
                "LLM-driven entry redundant for this run.", name)
        forced_remove.append((name, lid))
    if forced_remove:
        logger.info("forced --remove-label targets: %s",
                    ", ".join(n for n, _ in forced_remove))
    if catalog.removable:
        logger.info("removable catalog: %d labels available for LLM-proposed strip",
                    len(catalog.removable))

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
                                 counters, counters_lock,
                                 reviewed_label=reviewed_label,
                                 forced_remove=forced_remove)
                processed.update(t.thread_id for t in b)
                actions_since_confirm += len(b)
            _checkpoint(state_file, processed)
            report_progress()
            batch_buffer.clear()
            maybe_confirm()

        for t in client.search_threads(query, max_threads=limit,
                                        skip_ids=processed,
                                        include_body=include_body):
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
    default=None,
    help="Decision log to read already-kept emails from. Default: ./dry-run.log.",
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
    "--state-file", type=click.Path(path_type=Path), default=None,
    help="Resume checkpoint of relabeled IDs. Default: ./relabel-state.json.",
)
@click.option(
    "--log-file", type=click.Path(path_type=Path), default=None,
    help="Relabel log. Default: ./relabel.log.",
)
@click.option(
    "--remove-label",
    "remove_labels_forced",
    metavar="NAME",
    multiple=True,
    help="Unconditionally strip label NAME from every thread relabel "
         "touches. Repeatable. Issued in the same threads.modify as the "
         "label move when one is needed, or as a standalone modify when "
         "the label is unchanged. Pair with `removable:` in "
         "config/labels.yaml for LLM-proposed strips (which require "
         "--refetch-snippets to populate the thread's current labels). "
         "Off when omitted.",
)
def relabel(input_log, batch_size, llm_retries, apply, confirm_every,
            concurrency, refetch_snippets, state_file, log_file,
            remove_labels_forced):
    """Re-label already-kept emails against the current label catalog.

    Reads `keep` decisions from a prior decision log, re-asks the LLM to
    pick the best label for each from the *current* config/labels.yaml,
    and (with --apply) moves the Gmail label. It NEVER decides
    keep-vs-trash and never calls trash — emails that were kept stay
    kept. Use this after expanding your label catalog to reorganize
    without redoing the trash decisions.
    """
    work = _work_root()
    cfg = _resolve_config_dir()
    if input_log is None:
        input_log = work / "dry-run.log"
    if state_file is None:
        state_file = work / "relabel-state.json"
    if log_file is None:
        log_file = work / "relabel.log"

    catalog = LabelCatalog.load(cfg / "labels.yaml")
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

    # --remove-label resolution (same shape as classify).
    forced_remove: list[tuple[str, str]] = []
    for name in remove_labels_forced:
        lid = label_ids.get(name)
        if lid is None:
            logger.warning(
                "--remove-label %r is not a Gmail label in this account; "
                "ignoring.", name)
            continue
        forced_remove.append((name, lid))
    if forced_remove:
        logger.info("forced --remove-label targets: %s",
                    ", ".join(n for n, _ in forced_remove))
    if catalog.removable and not refetch_snippets:
        logger.info(
            "labels.yaml `removable:` catalog is set but --refetch-snippets is "
            "off; the relabel pass has no per-thread current-label list, so "
            "LLM-proposed removals will all fail validation. Pass "
            "--refetch-snippets to enable.")

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
            forced_remove_ids = [lid for _, lid in forced_remove]
            forced_remove_names = [n for n, _ in forced_remove]
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
                    # Combine forced + LLM-proposed removals (LLM-proposed
                    # already filtered to catalog.removable + on-thread
                    # by validate_relabel_decisions).
                    llm_remove_names = list(d.get("remove_labels") or [])
                    remove_names: list[str] = []
                    seen: set[str] = set()
                    for name in forced_remove_names + llm_remove_names:
                        if name in seen:
                            continue
                        seen.add(name)
                        remove_names.append(name)
                    changed = new_label != old_label
                    if apply and (changed or remove_names):
                        add_ids: list[str] = []
                        remove_ids: list[str] = list(forced_remove_ids)
                        if changed:
                            if new_label in label_ids:
                                add_ids.append(label_ids[new_label])
                            else:
                                logger.warning(
                                    "new label %r has no Gmail id; logging only",
                                    new_label)
                            if old_label in label_ids:
                                remove_ids.append(label_ids[old_label])
                        for name in llm_remove_names:
                            lid = label_ids.get(name)
                            if lid and lid not in remove_ids:
                                remove_ids.append(lid)
                        # Skip empty modifies (can happen when new_label
                        # has no Gmail id and no removals): treat as a
                        # log-only event.
                        if add_ids or remove_ids:
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
                                                 f"apply failed: {e}",
                                                 removed_labels=remove_names or None)
                                continue
                    with counters_lock:
                        counters["changed" if changed else "unchanged"] += 1
                    with log_lock:
                        _log_relabel(log_fh, rec, old_label, new_label, changed,
                                     "applied" if (changed and apply) else "",
                                     removed_labels=remove_names or None)
                    processed.add(rec["id"])
                    if apply and (changed or remove_names):
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
                    item["current_labels"] = list(meta.current_labels)
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


def _log_relabel(fh, rec: dict, old_label, new_label, changed: bool, note: str,
                 removed_labels: list[str] | None = None):
    out = {
        "id": rec["id"],
        "from": rec.get("from", ""),
        "subject": (rec.get("subject", "") or "")[:120],
        "old_label": old_label,
        "new_label": new_label,
        "changed": changed,
        "note": note,
    }
    if removed_labels:
        out["removed_labels"] = list(removed_labels)
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
                "body": t.body,
                "current_labels": list(t.current_labels),
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
    reviewed_label: str | None = None,
    forced_remove: list[tuple[str, str]] | None = None,
) -> None:
    """Apply (or just log) decisions for one batch. Runs on the main
    thread — Gmail client isn't thread-safe and we want stable log
    ordering.

    When `reviewed_label` is set, every KEPT email — LLM-kept and
    whitelisted alike — also receives that label, on top of any category
    label, so future runs can filter it out. Trashed and errored emails
    are never given the reviewed label.

    `forced_remove` is the resolved [(name, id), ...] list from
    --remove-label: those labels are stripped from every kept and
    whitelisted thread, regardless of LLM input. Trashed and errored
    threads are skipped — trash hides labels anyway. LLM-proposed
    removals come from each decision's optional `remove_labels` field
    (already validated against catalog.removable + current_labels in
    prompt.validate_decisions_strict) and stack with the forced ones.

    In `--apply` mode the decision is logged only AFTER the Gmail
    mutation is attempted. If the trash/label call raises, the whole
    record is logged as `action: "error"` (and counted as an error, not
    a keep/trash) so the log never claims a mutation that did not land,
    and `--retry-errors` can pick the thread up on a later run."""
    reviewed_id = label_ids.get(reviewed_label) if reviewed_label else None
    forced_remove = forced_remove or []
    forced_remove_ids = [lid for _, lid in forced_remove]
    forced_remove_names = [name for name, _ in forced_remove]

    # Whitelisted threads — emit KEEP-no-label decision. Possible Gmail
    # mutations: the reviewed-label (add) + the forced-remove labels.
    for t in whitelisted:
        err: str | None = None
        add_ids = [reviewed_id] if reviewed_id else []
        if apply and (add_ids or forced_remove_ids):
            try:
                client.modify_thread_labels(
                    t.thread_id,
                    add_label_ids=add_ids,
                    remove_label_ids=forced_remove_ids,
                )
            except Exception as e:
                err = str(e)
                logger.error("modify failed for %s: %s", t.thread_id, e)
        if err is not None:
            with counters_lock:
                counters["errors"] += 1
            note = ("reviewed-label apply failed: " + err[:200]
                    if reviewed_id and not forced_remove_ids
                    else "modify failed: " + err[:200])
            with log_lock:
                _log(log_fh, t, "error", None, note)
        else:
            with counters_lock:
                counters["whitelist"] += 1
            with log_lock:
                _log(log_fh, t, "keep", None, "whitelist",
                     reviewed_label=reviewed_label,
                     removed_labels=forced_remove_names or None)

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
            err = None
            if apply:
                try:
                    client.trash_thread(t.thread_id)
                except Exception as e:
                    err = str(e)
                    logger.error("trash failed for %s: %s", t.thread_id, e)
            if err is not None:
                with counters_lock:
                    counters["errors"] += 1
                with log_lock:
                    _log(log_fh, t, "error", None, f"trash failed: {err[:200]}")
            else:
                with counters_lock:
                    counters["trash"] += 1
                with log_lock:
                    _log(log_fh, t, "trash", None,
                         "" if not apply else "applied")
        else:  # keep
            label = d.get("label")
            # LLM-proposed strips (already validated against
            # catalog.removable + current_labels) + forced strips,
            # de-duplicated, preserving forced-first order.
            llm_remove_names = list(d.get("remove_labels") or [])
            remove_names: list[str] = []
            seen: set[str] = set()
            for name in forced_remove_names + llm_remove_names:
                if name in seen:
                    continue
                seen.add(name)
                remove_names.append(name)
            err = None
            if apply:
                add_ids: list[str] = []
                if label:
                    lid = label_ids.get(label)
                    if lid:
                        add_ids.append(lid)
                    else:
                        logger.warning("label %r has no Gmail id; skipping", label)
                if reviewed_id:
                    add_ids.append(reviewed_id)
                remove_ids: list[str] = []
                for name in remove_names:
                    lid = label_ids.get(name)
                    if lid:
                        remove_ids.append(lid)
                    else:
                        logger.warning("remove label %r has no Gmail id; skipping",
                                       name)
                if add_ids or remove_ids:
                    try:
                        client.modify_thread_labels(
                            t.thread_id,
                            add_label_ids=add_ids,
                            remove_label_ids=remove_ids,
                        )
                    except Exception as e:
                        err = str(e)
                        logger.error("label failed for %s: %s", t.thread_id, e)
            if err is not None:
                with counters_lock:
                    counters["errors"] += 1
                with log_lock:
                    _log(log_fh, t, "error", None,
                         f"label apply failed: {err[:200]}")
            else:
                with counters_lock:
                    counters["keep"] += 1
                with log_lock:
                    _log(log_fh, t, "keep", label,
                         "" if not apply else "applied",
                         reviewed_label=reviewed_label,
                         removed_labels=remove_names or None)


def _log(fh, t: ThreadSummary, action: str, label: str | None, note: str,
         reviewed_label: str | None = None,
         removed_labels: list[str] | None = None):
    rec = {
        "id": t.thread_id,
        "from": t.sender,
        "subject": t.subject[:120],
        "action": action,
        "label": label,
        "note": note,
    }
    if reviewed_label:
        rec["reviewed_label"] = reviewed_label
    if removed_labels:
        rec["removed_labels"] = list(removed_labels)
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fh.flush()


def _checkpoint(state_file: Path, processed: set[str]) -> None:
    state_file.write_text(json.dumps({"processed": sorted(processed)}))


@cli.command(name="apply-log", context_settings={"max_content_width": 100})
@click.option(
    "--log-file", type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Source of decisions to replay. Default: ./dry-run.log.",
)
@click.option(
    "--state-file", type=click.Path(path_type=Path),
    default=None,
    help="Resume checkpoint of applied IDs. Default: ./state-applied.json.",
)
@click.option(
    "--apply/--dry-run", default=False, show_default=True,
    help="--apply actually mutates Gmail. Default is dry-run preview.",
)
@click.option(
    "--limit", type=int, default=None,
    help="Apply at most this many actions (for staged rollout).",
)
@click.option(
    "--batch-size", type=int, default=applylog.BATCH_SIZE, show_default=True,
    help="Sub-requests per batch HTTP call. Higher = faster but more 429s "
         "(Gmail's per-user concurrent limit is ~20).",
)
@click.option(
    "--batch-sleep", type=float, default=1.5, show_default=True,
    help="Sleep between batches (seconds). Keeps us under Gmail's 250 QU/s/user quota.",
)
@click.option(
    "--credentials", type=click.Path(exists=True, path_type=Path),
    default=None,
    help="OAuth client secrets file. Default: $GMAIL_CLEANUP_CONFIG_DIR or "
         "./config/credentials.json.",
)
@click.option(
    "--token", type=click.Path(path_type=Path),
    default=None,
    help="OAuth token cache. Default: $GMAIL_CLEANUP_CONFIG_DIR or "
         "./config/token.json.",
)
@click.option(
    "--audit-log", type=click.Path(path_type=Path), default=None,
    help="Audit log. Default: ./applied.log under --apply, "
         "./replay-preview.log under --dry-run.",
)
def apply_log(log_file, state_file, apply, limit, batch_size, batch_sleep,
              credentials, token, audit_log):
    """Replay decisions from a dry-run.log to Gmail without re-classifying.

    Reads the log, keeps the latest decision per thread, and replays each
    via Gmail's batch HTTP API. Resumable: applied IDs are checkpointed
    to --state-file after every batch.
    """
    work = _work_root()
    cfg = _resolve_config_dir()
    if log_file is None:
        log_file = work / "dry-run.log"
    if state_file is None:
        state_file = work / "state-applied.json"
    if credentials is None:
        credentials = cfg / "credentials.json"
    if token is None:
        token = cfg / "token.json"
    if audit_log is None:
        audit_log = work / ("applied.log" if apply else "replay-preview.log")

    # Defer the exists check on --credentials until after our default
    # resolution so the error message is meaningful for users who
    # haven't run `auth` yet.
    if not Path(credentials).exists():
        raise click.UsageError(
            f"OAuth credentials file not found: {credentials}\n"
            "Either run `gmail-cleanup auth` to set up OAuth, or pass "
            "--credentials/--token explicitly, or set "
            "GMAIL_CLEANUP_CONFIG_DIR to a directory containing both files."
        )
    if not Path(log_file).exists():
        raise click.UsageError(f"Decision log not found: {log_file}")

    applylog.run_apply_log(
        log_file=log_file, state_file=state_file, apply=apply,
        limit=limit, batch_size=batch_size, batch_sleep=batch_sleep,
        credentials=credentials, token=token, audit_log=audit_log,
    )


def main():
    cli()


if __name__ == "__main__":
    main()
