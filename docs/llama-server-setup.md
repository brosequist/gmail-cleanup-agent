# Running against llama.cpp's `llama-server`

The `openai` backend works with llama.cpp's `llama-server` binary, but a
few llama.cpp-specific behaviors will silently destroy your throughput
or accuracy if you don't account for them. This document captures the
gotchas, all of them learned the hard way.

## Setup

llama.cpp's `llama-server` speaks the OpenAI Chat Completions wire
format, so config is the same as for any other OpenAI-compatible
endpoint:

```bash
export GCA_BACKEND=openai
export OPENAI_BASE_URL=http://localhost:8080/v1     # or wherever you bind
export OPENAI_API_KEY=not-needed                    # llama-server ignores it
export OPENAI_MODEL=<the model name llama-server advertises>
```

Find the exact model name by hitting `/v1/models`:

```bash
curl -s http://localhost:8080/v1/models | python3 -m json.tool
```

The `id` field is what to set `OPENAI_MODEL` to. By default it's the
GGUF file basename; if you launched with `--alias name`, that's what
shows up.

## Gotcha 1: Reasoning / thinking models

Qwen3, Qwen3.6, DeepSeek-R1, and other reasoning models split their
output between two fields:

- `message.content` — the final answer
- `message.reasoning_content` — the chain-of-thought

If the model spends its entire `OPENAI_MAX_TOKENS` budget on
`reasoning_content`, you get a response with `content: ""` and
`finish_reason: "length"`. The classifier sees no decisions and
flags every email in the batch as "missing".

**Symptom:** `WARNING gmail_cleanup: attempt 1: 20 emails missing
decisions, retrying just those` on every batch, immediately followed
by the retry-by-individual-email fallback (which also fails).

**Fix:** opt in to the new env var:

```bash
export OPENAI_DISABLE_THINKING=1
```

This sends `extra_body.chat_template_kwargs.enable_thinking=false`
with every request, which llama.cpp respects by routing the model
straight to the answer with no chain-of-thought. Real OpenAI ignores
unknown extras safely, but **some other OpenAI-compatible servers
(vLLM, older LM Studio builds) may reject the request entirely** —
keep this flag off unless you're talking to llama.cpp or know your
server handles unknown extras.

## Gotcha 2: Sampling configs that work for chat actively break verbatim-reproduction tasks

llama.cpp lets you set sampler config at launch time (`--temp`,
`--top-k`, `--repeat-penalty`, `--dry-multiplier`, `--dry-base`,
`--dry-allowed-length`, etc.). It's tempting to use sampling configs
tuned for natural-language quality (DRY anti-repetition, repetition
penalty, etc.) for ALL workloads.

That's wrong for this classifier.

This classifier's prompt includes 10-20 emails each prefaced with a
16-character hex thread ID, and the model has to reproduce **those
exact same IDs** in its JSON output. The same DRY / repeat-penalty
flags that prevent prose from looping ("I'll ensure I'll ensure …")
will actively push the model away from verbatim reproduction:

| Sampler at default | Observed on hex IDs |
|---|---|
| `--repeat-penalty 1.1 --repeat-last-n 256` | character substitutions (`1`→`l`, `3`→`e`) |
| `--dry-multiplier 0.8 --dry-allowed-length 8 --dry-penalty-last-n 1024` | truncation of the last 1–2 hex chars, spaces inserted into the middle, hallucinated tokens |

We observed 90 % of IDs hallucinated in some batches on a config tuned
for diagram-XML output. Fix: when running classify against a
llama-server backend that has these flags set, either

1. **Run a separate llama-server replica for classify** with vanilla
   sampling (no DRY, `--repeat-penalty 1.0`), OR
2. **Use a smaller `--batch-size`** (5–10 instead of 20) so each batch
   has fewer IDs for the sampler to penalize, OR
3. **Override per-request** — llama.cpp's OpenAI shim accepts
   `frequency_penalty: 0`, `presence_penalty: 0` in the request body;
   it doesn't currently accept arbitrary `dry_*` overrides via the
   OpenAI extras, but a future change to this backend could expose
   that.

Ollama's defaults don't include DRY, and its `repeat_penalty`
window is smaller, so Ollama-backed runs don't see this problem at
the same severity. If you have the choice and your model is small
enough for Ollama, prefer Ollama for classify-style tasks.

## Gotcha 3: `--parallel N` usually slows you down on a single GPU

llama-server's `--parallel N` opens N slots so it can serve N
requests concurrently. Pairing it with the classifier's
`--concurrency N` sounds like a clean throughput win.

It isn't, on a single GPU. The N slots all share the same compute
units, so per-slot throughput drops to roughly `1/N` of the
single-slot rate. We measured **~25× slower per-email** going from
`--parallel 1` to `--parallel 4` on a single 16 GB AMD card.

Two things can make `--parallel > 1` worthwhile:

- **Multiple distinct users** hitting the server (chat UI + classify
  running in parallel): the parallelism prevents head-of-line
  blocking even though each request runs slower. Net latency goes
  down for everyone even though total throughput is similar.
- **Multi-GPU setups** where llama.cpp can pin different slots to
  different physical GPUs. We haven't validated this.

For single-user, single-GPU classify runs: leave `--parallel 1` and
let the classifier's `--concurrency` queue requests at the
client side. (Higher classifier-side concurrency still helps for
amortizing client-side overhead, just don't expect linear
inference-side speedup.)

## Gotcha 4: `--cache-reuse` helps a lot for follow-up requests, less for the first batch

llama.cpp's `--cache-reuse N` lets the server reuse KV-cache slices
from a previous request when the current request shares a token
prefix with the cached one. The classifier prompt has a large fixed
prefix (rules.md + label catalog = ~6,000 tokens) that repeats
identically across every batch.

**This is a big win** — but only on batch 2+, and only on the same
slot. Each new batch starts fresh on slot 0; if you've used
`--parallel N`, batches 1..N each go to a fresh slot with no cache
to reuse.

If you stick with single-slot serial execution and add
`--cache-reuse 256` (the recommended token-chunk granularity), the
classifier's prompt-processing time drops by ~75 % from batch 2
onward. Combined with `--parallel 1` this is the recommended
single-GPU config for classify.

## Gotcha 5: Smaller `--batch-size` is more reliable on quantized models

For any quant level (Q4_K_M, Q5_K_M, IQ3_XXS, …), smaller batches
give the model fewer IDs to track per call, which lowers the rate of
ID-hallucination errors. The trade-off is more LLM calls per email,
so total wall-clock goes up a bit. For higher-precision quants
(Q5+ on quality llama.cpp builds) `--batch-size 20` is usually fine;
for Q4 or lower, `--batch-size 10` is the sweet spot.

If you see `WARNING gmail_cleanup: validate: unknown id: '...'` lines
where the ID is *almost* but not quite what you sent (character flips,
truncation, inserted whitespace), drop `--batch-size` and re-run.

## Reference: working config for classify on llama-server

```bash
# llama-server side (excerpt of launch args):
#   --parallel 1
#   --cache-reuse 256
#   No DRY flags (or a separate replica without them for classify)
#   --repeat-penalty 1.0   (or omit — default)

# Client side:
export GCA_BACKEND=openai
export OPENAI_BASE_URL=http://localhost:8080/v1
export OPENAI_API_KEY=not-needed
export OPENAI_MODEL=<from /v1/models>
export OPENAI_DISABLE_THINKING=1   # only for thinking models (Qwen3, R1, ...)

# For Q4 quants, drop batch size:
python -m gmail_cleanup classify --batch-size 10 --concurrency 4 --query ... --dry-run
```

## When you should NOT use llama-server for this task

If you have a comparable model available via Ollama, **prefer Ollama
for classify-style workloads**. Ollama's defaults are friendlier to
verbatim-reproduction tasks (no DRY, smaller repeat-penalty window),
and the absence of the reasoning_content / content split avoids the
`enable_thinking=false` dance.

llama-server's strengths (extensive sampling tuning, advanced KV
quantization, custom chat templates) are real but they shine in
chat-style and creative-generation workloads, not in
mechanical classify-and-extract tasks like this one.
