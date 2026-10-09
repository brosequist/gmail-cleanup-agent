# Cost math

The numbers in the [README](../README.md) come from one real run.
This doc shows the inputs so you can plug in your own. See
[results.md](results.md) for the full run breakdown.

## My run

- **Mailbox size:** 312,262 unique threads classified
  (Gmail's match total for the query was 312,982; small gap is threads
  that fell out of the 90-day window during the run).
- **Hardware:** AMD RX 9070 XT (16 GB) primary + RX 9060 XT (16 GB)
  offload, ROCm 7.2.2, on an Ubuntu host with a 24-core CPU.
- **Model:** `qwen3.6:35b-a3b-iq3_xxs-fixed` via Ollama
  (35B-A3B MoE, ~3B active params per token, IQ3 quant ≈ 15 GB).
- **Concurrency:** 4 parallel batches of 20 threads each (workers
  serialize on a single GPU via Ollama).
- **Active GPU time:** ~65 hours. This is the wall-clock during
  productive batches (sum of per-worker batch latencies divided by
  concurrency 4, since the same GPU-seconds would otherwise be
  counted 4 times). It does NOT include time the script was stopped,
  time spent waiting on a downed Ollama, or time spent on batches
  that failed without producing a decision. The script runs were
  spread across ~9 calendar days of mostly unattended operation
  with several maintenance interruptions, but that calendar span is
  not what counts against "cost to run."
- **Final outcome:** keep 53,099 (17.0%), trash 259,163 (83.0%),
  error 0 (after a single `--retry-errors` pass; mid-run error rate
  was ~8% peak, mostly during a period of Ollama instability).

## Per-batch token sizing

Re-measured on the 1.4 prompt (one-or-two-label contract), 2026-10-09.
The prompt is the real `build_prompt()` output for the shipped
`config/rules.example.md` + `config/labels.example.yaml`, with a
20-email batch of realistic sender / subject lengths and full
~200-character snippets. Tokens were counted with two real
tokenizers rather than estimated from characters:

- **`o200k_base`** (via `tiktoken`) — exact for GPT-4o / GPT-4.1
  family models, and the figure used for the cost table below.
- **Qwen3** (the `Qwen/Qwen3-8B` tokenizer, via `tokenizers`) — what a
  local Qwen model actually sees; a few percent higher.

Claude and Gemini use their own tokenizers, so treat their rows as
estimates.

| Item | o200k | Qwen3 |
|---|---:|---:|
| Fixed prefix (rules + label catalog + output-format instructions) | 3,517 | 3,580 |
| Per email (id + sender + subject + age + flags + snippet) | ~103 | ~115 |
| **Total input per batch of 20** | **5,586** | **5,888** |
| **Output per batch of 20** (`{"decisions":[...]}`, compact JSON) | **~530** | ~630 |

The prefix is about 63 % of every batch's input, and it is identical
across batches. That is why the prefix size matters more than anything
else: a longer `rules.md` raises every batch's cost by the same amount.
1.4's longer output-format section (the two-label rules) is most of the
growth over the 1.3 figures, alongside the per-email `Age:` and
`List-Unsubscribe:` lines.

With `--include-body` and bodies at the full 4 KB cap, the per-email
cost rises to **~810 tokens** (o200k), so a batch of 20 is about
19,700 tokens, roughly **3.5×** a snippet-only batch. Short bodies cost
proportionally less.

For 312,262 threads at batch size 20 = 15,614 batches:

- **Total input tokens:** 15,614 × 5,586 ≈ **87.2M**
- **Total output tokens:** 15,614 × 530 ≈ **8.3M**

If your own `rules.md` is longer or shorter than the example, the
prefix (and total cost) scales roughly linearly with it.

## Cloud API cost estimates

Using published rates as of early 2026; re-check current pricing
before quoting these:

| Backend | $/MTok in | $/MTok out | Input cost | Output cost | **Total** |
|---|---|---|---|---|---|
| Claude Haiku 4.5 | $1.00 | $5.00 | $87.20 | $41.50 | **~$129** |
| GPT-4.1-mini | $0.40 | $1.60 | $34.88 | $13.28 | **~$48** |
| GPT-4o-mini | $0.15 | $0.60 | $13.08 | $4.98 | **~$18** |
| Gemini 2.0 Flash | $0.10 | $0.40 | $8.72 | $3.32 | **~$12** |

These are full-price. The `claude` backend does not request Anthropic
prompt caching, so no caching discount applies there. Providers that
cache repeated prompt prefixes automatically (OpenAI does, above a
minimum prompt length) will bill the ~3,500-token prefix at their
cached-input rate on most batches, which can take a large share off
the input column; check your provider's current cached-input price.

The reasoning-model caveat applies to local runs too: a model that
"thinks" before answering emits far more output tokens than the table
assumes. See [llama-server-setup.md](llama-server-setup.md).

## Local-LLM electricity estimate

Rough — depends heavily on your hardware, region, and how busy the
machine is doing other things during the run.

- **System power under LLM load:** ~600 W (rough estimate: RX 9070 XT
  ~300 W under load + idle RX 9060 XT ~50 W + CPU/RAM/PSU losses
  ~150 W + overhead).
- **Runtime:** ~65 hours.
- **Energy:** 600 W × 65 h ≈ **39 kWh**.
- **US average residential rate:** ~$0.16/kWh
  ([EIA monthly data](https://www.eia.gov/electricity/monthly/epm_table_grapher.php?t=table_5_06_a)).
- **Total electricity cost:** ~**$6.24**.

The "$6" headline figure in the README rounds to one significant
digit, which is appropriate for an estimate like this. If your
electricity is $0.30/kWh (California, parts of the EU), double the
number; if it's $0.10/kWh (much of the US South), halve it.

## Manual-triage time estimate

Conservative: 3 seconds per email to read sender + subject and
decide. Less if you're just skimming for obvious trash; more if you
also assign labels.

- 312,262 emails × 3 sec ≈ 260 hours
- At 8 hrs/day: ~33 working days
- At 40 hrs/week: ~6.5 weeks of full-time work

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
  per-minute TPM more than by the model itself). For 312k threads
  that's roughly 60–100 minutes of wall-clock.
