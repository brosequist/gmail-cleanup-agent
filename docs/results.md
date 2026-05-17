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

This run was dry-run only (`--apply` was not used). Trash and label
operations have not been performed on the inbox; the decision log
serves as the audit trail for a later apply pass.
