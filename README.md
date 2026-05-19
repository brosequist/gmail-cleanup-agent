# gmail-cleanup-agent

Triage a Gmail inbox you've given up on. Uses a local (or cloud) LLM to
classify every old email as **important** (keep + label it) or
**non-important** (move to trash), with a full audit log and a
sender whitelist that always wins.

Built for the inbox-bankruptcy use case: you have 50k–500k unlabeled
emails, most of them marketing / political / automated, but real
things are mixed in (family, receipts, school, medical, government).
Manual triage is intractable; a hard-coded rules engine misses too
much; a generic AI tool either touches things it shouldn't or sends
your mail to a vendor. This script is a middle path.

## Why this exists

I had ~313k old emails I'd been ignoring for a decade
(see [docs/results.md](docs/results.md) for the full run breakdown).
The math is brutal:

| Path | Time | Money | Privacy |
|---|---|---|---|
| **Manual** (3 sec / email, average) | ~260 hrs ≈ 6.5 weeks full-time | $0 | full |
| **This script, local LLM** on a consumer GPU | ~65 hrs of active LLM time (4 worker threads serialize on a single GPU; idle / downtime / failed-call retries excluded) | ~$6 of electricity | full — nothing leaves the machine |
| **This script, Claude Haiku 4.5** | ~60–100 min | ~$69 | sender/subject/snippet leave the machine |
| **This script, GPT-4o-mini** | ~60–100 min | ~$10 | same |
| **This script, Gemini 2.0 Flash** | ~60–100 min | ~$7 | same |

(Token + cost math for the API rows is in [docs/cost-math.md](docs/cost-math.md).
Local runtime is from my actual run on an RX 9070 XT + RX 9060 XT running
`qwen3.6:35b-a3b` at IQ3, concurrency 4. Your mileage will vary.)

The script is conservative by default: **dry-run is the default**,
**trash is reversible for 30 days**, and there's a **sender whitelist**
that the LLM cannot override.

**Expect an iterative workflow.** A first pass typically clears the
bulk of unwanted mail (~83 % of threads in the example run), but a
post-hoc audit of the *kept* side usually surfaces a few-percent
tail of false-keeps — recurring digests, old sign-in alerts, social-
network reply notifications — that warrant a much shorter follow-on
run with tightened rules. See [docs/results.md](docs/results.md) for
the procedure and concrete numbers from one real run.

## Features

- **Local-LLM-by-default** via Ollama or LM Studio. No email content
  leaves your network unless you pick a cloud backend.
- **Pluggable backends** — Ollama, LM Studio (OpenAI-compatible),
  Anthropic Claude, or real OpenAI. Same script, four wire formats.
- **Batched classification** — ~20 emails per LLM call.
- **Resumable** — checkpoints after every batch to `state.json`.
  Ctrl+C and re-run with the same args; already-classified threads
  are skipped at Gmail-list speed.
- **Dry-run by default** — every run produces a decision log first;
  nothing touches Gmail until you pass `--apply`.
- **Trash, not permanent delete** — Gmail's 30-day recovery window
  protects against false positives.
- **Sender whitelist** — addresses or domains that are *never*
  classified as trash, regardless of LLM judgment.
- **Auto-create labels** — for important categories that don't exist
  yet, the agent creates them based on your `labels.yaml` catalog.
- **Audit trail** — every decision logged with sender, subject,
  action, label, and reason.

## What gets sent to the LLM

For each email:

- Sender (`From:` header)
- Subject
- Snippet (the ~200-char preview Gmail provides; capped at 300 chars
  defensively by the prompt builder)
- **Age** in days (derived from Gmail's `internalDate`). Lets the
  classifier apply different heuristics for "received last week" vs
  "received 5 years ago" — especially useful for inbox-bankruptcy
  on years-old mail where time-sensitive notices (sign-in alerts,
  verification codes, expired sales) are no longer actionable.
- **`List-Unsubscribe: yes`** flag (RFC 2369). Personal mail almost
  never has this; bulk / automated / marketing mail almost always
  does. Very strong "this is automated" signal.
- **Never the full body** unless you explicitly pass `--include-body`
  (off by default — and even then, snippet is preferred when
  available).
- **Never the exact Date.** Only the derived age-in-days makes it
  into the prompt, not the raw timestamp.

See [docs/privacy.md](docs/privacy.md) for the full breakdown.

## Labels: how kept mail gets organized

Every "keep" decision is also a **label-classification decision**. The
LLM doesn't just decide *whether* an email survives — it picks the
single best-matching label from a catalog you provide, and the script
applies that label in Gmail. After a run, every kept email is filed
under a specific category instead of sitting unsorted in your inbox.

The catalog lives in
[`config/labels.yaml`](#configlabelsyaml) and has two parts:

```yaml
existing:           # labels that already exist in your Gmail
  - Taxes
  - Filed
auto_create:        # categories the agent will create the first time
  Family:        "personal correspondence from friends or family members"
  Receipts:      "order confirmations, purchase receipts, ..."
  Statements:    "monthly/quarterly account statements ..."
  Medical:       "doctors, hospitals, pharmacies, lab results, EOBs"
  Government:    "DMV, IRS, immigration, voter, courts, social security"
  Registrations: "event tickets, account creations, program enrollments"
  # ...etc
```

The one-line descriptions go into the prompt as part of the catalog
section, so the model knows what each label means without you having
to write explicit rules for every category in
[`config/rules.md`](#configrulesmd).

A real run's outcome (per [docs/results.md](docs/results.md)):

| Label | Threads kept |
|---|---:|
| Receipts | 21,793 |
| Registrations | 11,640 |
| Organizations | 3,523 |
| School | 3,348 |
| Family | 2,721 |
| Medical | 1,844 |
| Government | 1,440 |
| Statements | 1,116 |
| Veterans | 488 |
| … | … |
| **Total kept (across 18 labels)** | **53,099** |

A few label-system properties worth knowing up front:

- **The model is forced to pick exactly one label per kept email** —
  no comma-separated multi-labels, no "Misc/Other" fallback.
  [`config/rules.md`](config/rules.example.md) includes a "Don't
  reach for a catch-all label" principle: if no specific label
  clearly fits, prefer trash. This keeps the label set tight.
- **Labels you don't include in the catalog won't be assigned.** The
  model can only choose from the catalog you gave it.
- **You can reorganize later without re-classifying.** If after a
  run you want to add a new category — splitting `Receipts` →
  `Receipts` + `Travel` for booked-trip records, say — the
  [`relabel`](#reorganizing-later-the-relabel-pass) subcommand
  re-asks the model only for the label decision, never the
  keep-vs-trash decision. Already-kept mail stays kept.
- **Tighten the catalog before the big run.** Each label adds tokens
  to every batch's prompt; bloated catalogs cost real money on cloud
  backends and slow down local runs. See the design tips in
  [`config/labels.example.yaml`](config/labels.example.yaml).

## Quick start — local LLM

You need Python 3.11+ and a local LLM server. Pick one:

### Option A: Ollama

```bash
# 1. Install Ollama from https://ollama.ai, then pull a model
ollama pull qwen3:8b              # 5 GB, runs on most modern GPUs
# or, larger / smarter:
ollama pull qwen3.6:35b-a3b       # 15 GB, needs 16+ GB VRAM, much better quality

# 2. Make sure Ollama is serving (default: localhost:11434)
ollama serve &   # or run `ollama` as a system service

# 3. Install this tool
git clone https://github.com/brosequist/gmail-cleanup-agent
cd gmail-cleanup-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 4. Configure — copy the template and uncomment the Ollama section
cp config/backend.env.example config/backend.env
# edit config/backend.env: uncomment GCA_BACKEND / OLLAMA_HOST / OLLAMA_MODEL
# (you can leave the other backend sections commented out)
```

`config/backend.env` is loaded automatically every time you run
`python -m gmail_cleanup ...`. Shell-exported env vars still override
what's in the file, so one-off overrides (e.g.
`OLLAMA_MODEL=qwen3.6:35b-a3b python -m gmail_cleanup classify ...`)
work without editing the file.

### Option B: LM Studio

```bash
# 1. Install LM Studio from https://lmstudio.ai, download a model
#    inside it (Qwen 2.5 7B Instruct or Qwen 3 8B both work great),
#    then start its local server (Developer tab → Start Server).
#    Default URL: http://localhost:1234

# 2. Install this tool (same as above)
git clone https://github.com/brosequist/gmail-cleanup-agent
cd gmail-cleanup-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt openai     # +openai SDK for the
                                            # OpenAI-compatible client

# 3. Configure — LM Studio speaks the OpenAI wire format
cp config/backend.env.example config/backend.env
# edit config/backend.env: uncomment the "OpenAI-compatible" block and set
#   OPENAI_BASE_URL=http://localhost:1234/v1
#   OPENAI_API_KEY=not-needed             # placeholder; LM Studio doesn't check
#   OPENAI_MODEL=qwen2.5-7b-instruct      # exact id from LM Studio's Server tab
```

The same `GCA_BACKEND=openai` setup also works with llama.cpp's
`server` binary, vLLM, and Ollama's `/v1` OpenAI shim — point
`OPENAI_BASE_URL` accordingly.

If you're using **llama.cpp's `llama-server`**, there are a few
sharp edges worth knowing about — sampling configs that work great
for chat actively break verbatim-reproduction tasks like this one,
reasoning models (Qwen3, DeepSeek-R1, ...) need
`OPENAI_DISABLE_THINKING=1`, and `--parallel N` on a single GPU
typically *slows things down*. See
[docs/llama-server-setup.md](docs/llama-server-setup.md) for the
full set of gotchas.

If your backend runs in **Kubernetes** or behind an **SSH tunnel**,
the CLI can launch the port-forward / tunnel command for you and
clean it up on exit — see the `PRE_RUN_COMMAND` block in
[`config/backend.env.example`](config/backend.env.example). Useful
to avoid the `kubectl port-forward -n ollama svc/ollama 11434:11434 &`
ritual before every run.

## Quick start — cloud LLM (faster, costs money)

```bash
# Install the SDK for whichever provider you're using
pip install -r requirements.txt anthropic    # for Claude
# or
pip install -r requirements.txt openai       # for real OpenAI

# Configure via the env file
cp config/backend.env.example config/backend.env
```

Then edit `config/backend.env` and uncomment the **Anthropic Claude**
section (set `ANTHROPIC_API_KEY` + `CLAUDE_MODEL`) **or** the
**OpenAI-compatible** section pointed at real OpenAI (set
`OPENAI_API_KEY` to your real key + `OPENAI_MODEL=gpt-4o-mini` or
`gpt-4.1-mini`). Leave `OPENAI_BASE_URL` at the default (or unset) to
hit `api.openai.com`.

## Set up OAuth + run

```bash
# 1. One-time: create Google OAuth credentials (see docs/oauth-setup.md)
#    Save the JSON as config/credentials.json.

# 2. Configure your label catalog and classification rules
cp config/labels.example.yaml config/labels.yaml
cp config/rules.example.md config/rules.md
cp config/whitelist.example.txt config/whitelist.txt
# Edit each to match your priorities.

# 3. Authorize the tool against your Gmail account (browser pops once)
python -m gmail_cleanup auth

# 4. Dry-run on a small sample to sanity-check
python -m gmail_cleanup classify \
  --query "older_than:90d -has:userlabels" \
  --limit 100 --dry-run
# Review dry-run.log — every decision is recorded.

# 5. If it looks good, run for real
python -m gmail_cleanup classify \
  --query "older_than:90d -has:userlabels" \
  --apply
```

The bundled `scripts/run-cleanup.sh` wraps this with sensible
defaults and a tee'd log.

Every subcommand has built-in help — `python -m gmail_cleanup
--help` lists subcommands, and `python -m gmail_cleanup <subcommand>
--help` shows all options for that subcommand:

```bash
python -m gmail_cleanup --help          # auth | classify | apply-log | relabel
python -m gmail_cleanup classify --help # --query, --apply, --dry-run, --concurrency, ...
```

## Applying decisions from a dry-run: the `apply-log` pass

After a dry-run produces `dry-run.log`, you can replay its decisions
to Gmail without re-running the LLM. Useful when:

- You want to audit `dry-run.log` first, then commit the result
  later (or on a different machine) — no need to keep the GPU around.
- Your apply session got interrupted; `state-applied.json`
  remembers what's done and the next invocation picks up where it
  left off.
- You re-ran classify on the same threads, the rules improved, and
  you want to apply only the *latest* decision per thread.

```bash
# Preview what apply-log will do without touching Gmail
python -m gmail_cleanup apply-log --dry-run

# Actually apply (mutates Gmail; resumable via state-applied.json)
python -m gmail_cleanup apply-log --apply

# See every option
python -m gmail_cleanup apply-log --help
```

Key properties:

- **No LLM call.** It just reads the JSONL log and translates each
  row into a Gmail batch HTTP request — trash for `action: "trash"`,
  `threads.modify(addLabelIds=...)` for `action: "keep"` + label.
- **Latest decision wins.** Multiple log lines with the same thread
  id collapse to the most recent one. Re-running classify and then
  apply-log is safe.
- **Resumable** via `state-applied.json` — successful IDs are
  checkpointed after every batch.
- **Robust to 429s** — Gmail's per-user concurrent ceiling
  (~3.3 ops/sec) is hit easily; the subcommand retries 429ed
  requests with exponential backoff within each batch.

## Reorganizing later: the `relabel` pass

After a big classification run you'll often want to *add* label
categories — you notice 1,000 emails landed in `Receipts` that are
really travel itineraries, so you add a `Travel` label. The `relabel`
subcommand reorganizes **already-kept** mail against your updated
`config/labels.yaml` without redoing any keep-vs-trash decisions:

```bash
# Add the new categories to config/labels.yaml first, then:

# Dry-run — proposes label changes, writes relabel.log, touches nothing
python -m gmail_cleanup relabel --input-log dry-run.log --dry-run

# Review relabel.log — each line shows old_label → new_label + changed flag

# Apply — moves the Gmail label for every email whose label changed
python -m gmail_cleanup relabel --input-log applied.log --apply
```

Key properties:

- **It cannot trash anything.** `relabel` only ever assigns a label.
  The LLM is never asked to decide keep-vs-trash; emails that were
  kept stay kept.
- **It reads from a decision log** (`dry-run.log` or `applied.log`),
  taking only the `keep` rows. Trash and error rows are ignored.
- **On `--apply`** it does a single `threads.modify` per changed
  email: add the new label, remove the old one. Emails whose label
  didn't change get no API call.
- **Resumable** via its own `relabel-state.json` (separate from the
  classify checkpoint), so Ctrl+C and re-run is safe.
- **`--refetch-snippets`** re-pulls each email's snippet from Gmail
  for richer context — slower (one API call per email) but produces
  noticeably better labels than sender + subject alone.

## Configuration

### `config/labels.yaml`

Maps Gmail labels to importance categories. The agent uses existing
labels first, then creates new ones from the `auto_create` list as
needed.

```yaml
existing:
  - Taxes
  - Filed
  - Notes
auto_create:
  Family:        # personal correspondence
  Receipts:      # invoices, order confirmations
  Medical:       # healthcare providers
  School:        # educational institutions
  Sports:        # league registrations, schedules
  Organizations: # community / professional groups
  Government:    # government offices, tax authorities
  Registrations: # event / account / program confirmations
```

### `config/rules.md`

Free-form classification rules in plain English. The prompt builder
embeds it verbatim, so write to the LLM in plain language: *"emails
from any school or daycare are always keep"*, *"discount codes alone
are trash unless from a vendor I already buy from"*, etc.

### `config/whitelist.txt`

One sender per line. Anything matching is always kept; the LLM never
sees these and cannot override.

```
@yourdomain.com
mom@example.com
school.edu
```

## Safety

- **Trash, not permanent delete.** Gmail keeps trashed mail for 30
  days; recovery is one click in the UI.
- **Sender whitelist always wins over LLM judgment.**
- **`--dry-run` is the default for `classify`.** You must explicitly
  pass `--apply` for any state change.
- **Every run produces a `.log` file.** Loss-of-mail is auditable.
- **The script never modifies INBOX label state on already-labeled
  threads** — your existing organization is preserved.
- **OAuth scope is `gmail.modify`** — enough to trash and label, not
  enough to permanently delete or send mail.

## Privacy

See [docs/privacy.md](docs/privacy.md) for what data goes where.

Short version:

- **Local backends (Ollama, LM Studio):** no email content leaves
  your machine. The script makes Gmail API calls (Google sees what
  it always sees) and posts prompts to your local LLM server.
- **Cloud backends (Claude, OpenAI):** sender + subject + snippet
  for each email are sent to the chosen API. No full bodies unless
  you opt in with `--include-body`. No attachments. No headers
  beyond From/Subject/Date.

## Model selection

For local backends, the script uses each provider's JSON-output mode
to make sure the LLM returns parseable decisions:

- Ollama: `format: "json"`
- OpenAI-compatible (LM Studio, llama.cpp, vLLM, OpenAI):
  `response_format: {type: "json_object"}` (toggleable via
  `OPENAI_JSON_MODE=0` if your server doesn't support it).

| Model | Backend | Quality | Speed (concurrency 4) |
|---|---|---|---|
| `qwen3.6:35b-a3b` (IQ3) | Ollama | excellent | ~80 emails/min on RX 9070 XT + RX 9060 XT |
| `qwen3:8b` | Ollama | good | ~120 emails/min on a 12 GB GPU |
| `qwen2.5-7b-instruct` | LM Studio | good | similar to above |
| `claude-haiku-4-5` | Claude | excellent | ~3,000–5,000 emails/min |
| `gpt-4o-mini` | OpenAI | good | ~2,000–4,000 emails/min |

Smaller models still work but produce more "missing decision" retries
and more `error`-marked rows in the audit log. Anything ≥7B with
strong JSON-output discipline should be fine.

## Architecture

```
gmail_cleanup/
├── cli.py              entry point + command parsing + orchestration
├── gmail_client.py     thin wrapper over googleapiclient with retry
├── prompt.py           builds the LLM prompt from rules.md + labels.yaml
└── backends/
    ├── ollama.py       local Ollama server (HTTP JSON-mode)
    ├── openai.py       OpenAI-compatible (LM Studio, llama.cpp, real OpenAI)
    └── claude.py       Anthropic Claude
```

The pipeline:

1. `gmail_client.search_threads()` paginates `threads.list` with the
   user's query. Resume-mode skips IDs already in `state.json` before
   making the per-thread metadata fetch.
2. For each new thread, `messages.get(format=metadata)` retrieves
   From / Subject / Date.
3. Threads are batched (20 each), passed to `prompt.build_prompt()`,
   and sent to the configured backend's `classify_batch()`.
4. The JSON response is validated; missing decisions are retried up
   to 2× per batch.
5. Decisions are logged to `dry-run.log` (or `applied.log` with
   `--apply`), and threads are trashed or labeled.
6. `state.json` is checkpointed after every batch.

A ThreadPoolExecutor runs `--concurrency` batches in parallel. The
LLM client is shared across workers via a connection pool.

## Limitations

- **Threads, not individual messages.** The unit of decision is the
  thread. If a thread has a useful reply buried in promotional noise,
  the snippet-based classifier may still call it trash. Use
  `--include-body` for higher-stakes runs.
- **English-only rules out of the box.** The prompt is English; the
  model handles multilingual senders fine but your `rules.md` should
  be English unless your model is multilingual-tuned.
- **No spam-folder integration.** The script only looks at the
  mailbox you point it at. It won't read or rescue from Spam.
- **Sequential thread enumeration.** Gmail's `threads.list` is
  single-threaded; resume scans of huge state files still take a few
  minutes of pure list-pagination time.

## License

MIT — see [LICENSE](LICENSE).
