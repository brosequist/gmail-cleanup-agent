# Cost math

The numbers in the [README](../README.md) come from one real run.
This doc shows the inputs so you can plug in your own.

## My run

- **Mailbox size:** 230,760 threads classified (still running at time
  of writing — that's the count in `state.json`).
- **Hardware:** AMD RX 9070 XT (16 GB) primary + RX 9060 XT (16 GB)
  offload, ROCm 7.2.2, on an Ubuntu host with a 24-core CPU.
- **Model:** `qwen3.6:35b-a3b-iq3_xxs-fixed` via Ollama
  (35B-A3B MoE, ~3B active params per token, IQ3 quant ≈ 15 GB).
- **Concurrency:** 4 parallel batches of 20 threads each.
- **Wall-clock LLM time (sum of all batch durations / concurrency):**
  ~43 hours, spread across ~5 calendar days of intermittent runtime
  (the script was restarted several times during cluster maintenance).
- **Outcome at time of snapshot:** keep 36,413 (15.8%), trash
  170,627 (73.9%), error 23,720 (10.3% — mostly accumulated during a
  period of Ollama instability; effective error rate during stable
  operation is well under 1%).

## Per-batch token sizing

Measured by running the actual `build_prompt()` against a 20-email
batch with realistic sender / subject / snippet lengths and counting
the characters in the result (1 token ≈ 4 chars for English):

| Item | ~tokens |
|---|---|
| Fixed prefix (rules.md + label catalog + JSON-format instructions) | 1,670 |
| Per email (id + sender + subject + snippet, formatted) | 106 |
| **Total input per batch of 20** | **~3,790** |
| **Output per batch of 20** (`{"decisions":[...]}`) | **~200** |

For 230,760 threads at batch size 20 = 11,538 batches:

- **Total input tokens:** 11,538 × 3,790 ≈ **43.7M**
- **Total output tokens:** 11,538 × 200 ≈ **2.3M**

## Cloud API cost estimates

Using published rates as of early 2026; you should re-check current
pricing before quoting these:

| Backend | $/MTok in | $/MTok out | Input cost | Output cost | **Total** |
|---|---|---|---|---|---|
| Claude Haiku 4.5 | $1.00 | $5.00 | $43.70 | $11.50 | **~$55** |
| GPT-4.1-mini | $0.40 | $1.60 | $17.48 | $3.68 | **~$21** |
| GPT-4o-mini | $0.15 | $0.60 | $6.55 | $1.38 | **~$8** |
| Gemini 2.0 Flash | $0.10 | $0.40 | $4.37 | $0.92 | **~$5** |

These are full-price; in practice you'd also benefit from prompt
caching on the fixed prefix (the rules + label catalog are the same
across every batch in a run), which would knock another ~30–40% off
the input cost on providers that support it (Claude, OpenAI).

## Local-LLM electricity estimate

Rough — depends heavily on your hardware, region, and how busy the
machine is doing other things during the run.

- **System power under LLM load:** ~600 W (rough estimate: RX 9070 XT
  ~300 W under load + idle RX 9060 XT ~50 W + CPU/RAM/PSU losses
  ~150 W + overhead).
- **Runtime:** ~43 hours.
- **Energy:** 600 W × 43 h ≈ **25.8 kWh**.
- **US average residential rate:** ~$0.16/kWh
  ([EIA monthly data](https://www.eia.gov/electricity/monthly/epm_table_grapher.php?t=table_5_06_a)).
- **Total electricity cost:** ~**$4.13**.

The "$4" headline figure in the README rounds to one significant
digit, which is appropriate for an estimate like this. If your
electricity is $0.30/kWh (California, parts of the EU), double the
number; if it's $0.10/kWh (much of the US South), halve it.

## Manual-triage time estimate

Conservative: 3 seconds per email to read sender + subject and
decide. Less if you're just skimming for obvious trash; more if you
also assign labels.

- 230,760 emails × 3 sec ≈ 192 hours
- At 8 hrs/day: ~24 working days
- At 40 hrs/week: ~5 weeks of full-time work

Most people in this situation will simply never do it manually,
which is why the "manual" row of the README's headline table
includes the implicit caveat that you'd actually do it. The realistic
manual alternative for most people is *delete it all and start over*,
which loses the receipts / family / school messages mixed in.

## Throughput on the same hardware

If you want to estimate your own run:

- A 35B-A3B IQ3 MoE model on an RX 9070 XT + RX 9060 XT, concurrency
  4, averages ~80 classified emails/minute (each batch is ~60 s of
  wall time; 4 batches in flight × 20 emails / 60 s).
- A 7–8B dense model on a 12 GB GPU typically does ~120/min at
  similar concurrency.
- Claude Haiku 4.5 over a residential broadband connection averaged
  ~3,000–5,000 emails/min in my testing (rate-limited by Anthropic's
  per-minute TPM more than by the model itself).
