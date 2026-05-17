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

- Late-stage steady-state: ~80 emails / minute (~1.33 / sec)
- Per-batch latency observed in the console log spanned 21s–82s for
  20-email batches — variance dominated by individual emails with
  longer snippets and by occasional model-retry overhead.

The numbers above are the late-stage rate. Earlier sessions
were a mix of faster batches and idle resume-scan time, so this
isn't a clean compute-hours number — see the README's cost table
for the approximate active-compute figure.

## Apply pass

This run was dry-run only (`--apply` was not used). Trash and label
operations have not been performed on the inbox; the decision log
serves as the audit trail for a later apply pass.
