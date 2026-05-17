# Run results

Results from running this tool against my own Gmail inbox over
8–9 calendar days in May 2026. Machine-readable version of the
same numbers lives in [run-stats.json](run-stats.json).

## Headline

- **Gmail match total:** 312,982 threads
  (query: `older_than:90d -has:userlabels -in:trash -in:spam`)
- **Unique threads classified:** 312,262 (99.77% of match total — the
  small gap is threads that fell out of the 90-day window or were
  modified between Gmail's listing and the classifier's fetch)
- **Final decisions:** 259,163 trash (83%) · 53,099 keep (17%) · 0 errors
- **Errors after a single retry pass:** 0

## Final breakdown

| Action | Threads | Share |
|---|---:|---:|
| Trash | 259,163 | 83.0% |
| Keep  | 53,099  | 17.0% |
| Error (final) | 0 | 0.0% |
| **Total** | **312,262** | **100%** |

## Timeline

- **First session:** 2026-05-08 21:14 UTC
- **Last session:** 2026-05-17 15:48 UTC
- **Sessions:** 31 (resume-friendly — Ctrl+C and re-run was used often)
- **Calendar span:** ~8.8 days (mostly idle; classifier was only running
  during the user's working hours plus a couple of overnight passes)

## Retry pass

After the main classification was done, a single targeted pass with
`--retry-errors` was run against threads whose latest recorded action
was `error`:

- 809 threads retried
- All 809 resolved (144 keep, 665 trash)
- 0 remaining errors after the pass

## Model and hardware

- **Backend:** Ollama
- **Model:** `qwen3.6:35b-a3b-iq3_xxs-fixed` (IQ3_XXS quantization)
- **Batch size:** 20 emails per LLM call
- **Concurrency:** 4 in-flight batches
- **GPU:** AMD Radeon RX 9070 XT (Navi 48, 16 GB) +
  AMD Radeon RX 9060 XT (Navi 44, 16 GB) on rog-mega-pc
- **Stack:** Ubuntu 24.04 + ROCm 7.2.2

## Observed throughput

- Late-stage steady-state: ~80 emails / minute (~1.33 / sec wall-clock)
- Per-batch latency: mean 56.5s, median 48.7s, min 2.4s, max 1551s
  (20-email batches; variance dominated by individual emails with
  longer snippets and by occasional model-retry overhead)

## Compute time

Reconstructed by parsing every `classified N emails in Xs` line in
`dry-run.console.log` (covers 2026-05-09 → 2026-05-13). Post-May-13
sessions weren't tee'd to the console log, so their compute is
extrapolated from the measured per-email cost.

| Metric | Measured (console range) | Extrapolated (post-May 13) | Total |
|---|---:|---:|---:|
| Emails classified | 205,926 | 106,336 | 312,262 |
| Active GPU time | 43.0 h | 22.2 h | **~65 h** |
| Avg cost per email | 0.75 s | 0.75 s | — |

The four worker threads issue batches concurrently, but Ollama
serializes them on a single GPU, so the GPU-busy time is the
wall-clock time during productive batches — not the sum of per-worker
batch latencies (which would 4× count the same GPU-seconds because
three workers are sitting in the queue while one is being served).

The reported per-batch latency (mean 56.5s, median 48.7s) is therefore
queue-wait + GPU compute per worker; dividing by concurrency=4 gives
the actual per-email GPU cost.

**Sanity check:** the late-stage live throughput of 1.33 emails/sec
wall-clock matches the console-range average exactly, so the
extrapolation is self-consistent.

**What's excluded:** idle resume-scan time at the start of each
session, downtime between sessions, and failed-call retries that
didn't end up writing a batch-duration log line.

## Apply pass

After the dry-run was validated, the decisions were applied to Gmail
via `scripts/apply_from_log.py --apply`. The script reads the
existing `dry-run.log`, takes the latest decision per thread ID,
and replays each via the Gmail batch HTTP API — no LLM calls, no
re-classification. Resumable via `state-applied.json` and audited
in `applied.log`.

- **Wall-clock:** ~28 hours at ~3.1 ops/sec (Gmail's per-user
  processing ceiling on a single account, not a client-side limit)
- **API cost:** $0 (no LLM calls; well within Gmail's free quota)
- **Recovery window:** all trashed threads remain in Gmail's Trash
  for 30 days

## Follow-on (iterative refinement)

The initial classification removed the overwhelming majority of the
backlog of unwanted email — **259,163 of 312,262 threads (83 %)** were
moved to Trash in the apply pass. However, a post-hoc audit of the
remaining 53,099 "keep" decisions found roughly **1,800 threads
(3.4 % of the keeps) that were over-cautious calls** — i.e., further
trash candidates the first pass missed:

| Pattern | Count | Why the first pass kept them |
|---|---:|---|
| Old sign-in / verification-code notices | 746 | Time-sensitive at arrival; rules said "never trash a verification code" |
| Recurring daily/weekly digests (e.g. Vet Tix) | 588 | Subjects referenced label-worthy categories (event registrations) |
| LinkedIn job alerts | 286 | Look like personal-relevance "job opportunity for you" |
| Social-network reply notifications (Reddit, Nextdoor) | 164 | Snippet often quotes the original post; *looks* personal |
| **Total** | **~1,800** | |

Two things were done in response:

1. **Tool changes** (committed in [feat(classify): pass email age and
   List-Unsubscribe presence to model](../README.md#what-gets-sent-to-the-llm)) —
   the per-email prompt block now includes `Age: N days` (derived from
   Gmail's `internalDate`) and `List-Unsubscribe: yes` (RFC 2369
   header presence). Together these cover the missing signal that
   the original pass needed.
2. **`rules.example.md` updates** — explicit rules for each false-keep
   pattern, leveraging the new metadata fields:
   - sign-in / verification notices with `Age > 30 days` → TRASH
   - recurring digests → TRASH regardless of category
   - social-network reply notifications → TRASH unconditionally

A **follow-on classification run** then re-processes only the
previously-kept threads. Because it's roughly 1/6th the scope of the
original run (53k vs 312k), it's correspondingly faster — typically
**under 12 hours on the same hardware**, vs the original ~65 hours.

The procedure:

1. Update `config/rules.md` to incorporate the new metadata-aware
   rules (see `config/rules.example.md` for the patterns).
2. Move `state.json` aside so the classifier doesn't resume-skip
   everything: `mv state.json state.json.first-pass`.
3. Restrict the Gmail query to threads carrying the labels the first
   pass applied — for example `older_than:90d label:Receipts OR
   label:Registrations OR label:Notes -in:trash -in:spam`.
4. Run `classify --dry-run` (it'll be much shorter — ~12 hrs at the
   measured throughput).
5. Audit the new dry-run.log diff against the first pass, then
   `apply_from_log.py --apply` the deltas.

Most "kept" threads are correctly kept; the follow-on flips only the
~1,800-ish items per the categories above. The iterative-refinement
pattern is the expected workflow: dry-run → audit → apply → audit
the kept side → tighter rules → smaller follow-on. Each loop is
cheaper than the last.
