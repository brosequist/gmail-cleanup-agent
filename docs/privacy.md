# Privacy: what data goes where

This tool reads emails from your Gmail account, sends some of their content
to an LLM for classification, and applies labels / moves to trash based on
the LLM's decision. This page details exactly what data leaves your local
machine and where it goes.

## What is sent to the LLM

For each email, only:

- **Sender** (From: header)
- **Subject** line
- **Snippet** — the ~200-char preview Gmail returns by default
  (capped at 300 chars by the prompt builder as a defensive measure)
- **Age** in days — derived from Gmail's `internalDate` and rendered
  as `Age: N days`. The raw timestamp is **not** sent; only the
  derived day count. Lets the classifier apply different heuristics
  for "received last week" vs "received 5 years ago" (especially
  useful for inbox-bankruptcy on years-old mail where time-sensitive
  notices are no longer actionable).
- **`List-Unsubscribe: yes`** — a single boolean flag indicating
  presence of the RFC 2369 header. The header *value* (which can
  contain mailto: links or one-click URLs) is **not** sent.

The **full message body is NOT sent** unless you explicitly pass
`--include-body`. When set, the classifier fetches the first
message's `text/plain` part (falling back to `text/html` with tags
stripped) and includes up to 4 KB per email in the prompt. Off by
default — snippet alone is enough for the marketing-vs-personal
call this tool is making, and `--include-body` roughly triples
per-email prompt size on cloud backends.

Threads are processed independently — no conversation history is sent
across calls.

## Where the LLM runs

Depends on `GCA_BACKEND`:

### `ollama` (default, recommended)

Calls go to your local or LAN Ollama instance over HTTP. **No email
content leaves your network**. The default `OLLAMA_HOST` is
`http://localhost:11434`.

If you're running Ollama on a different machine on your LAN/Tailnet, set
`OLLAMA_HOST=http://<host>:11434`. Still local; still private.

### `claude`

Calls go to `https://api.anthropic.com`. Anthropic's terms apply. Per
their [usage policies](https://www.anthropic.com/legal/usage-policy) and
[privacy policy](https://www.anthropic.com/legal/privacy):

- API inputs are not used to train Anthropic's models by default.
- Inputs may be retained for safety/operations purposes; see Anthropic's
  current data retention policy.

If you want to use Claude with **zero retention**, contact Anthropic for
a Zero Retention Agreement (typically for enterprise customers).

### `openai`

The `openai` backend is OpenAI-wire-format-compatible. Where the calls
go depends entirely on `OPENAI_BASE_URL`:

- **Real OpenAI** (`https://api.openai.com/v1`, the default): OpenAI's
  terms apply. By default, OpenAI retains API inputs for 30 days
  unless you have a Zero Data Retention agreement.
- **LM Studio** (e.g. `http://localhost:1234/v1`): runs entirely on
  your local machine. **No email content leaves your network.** Same
  privacy posture as the Ollama backend.
- **llama.cpp server, vLLM, Ollama's `/v1` shim, or any other
  self-hosted OpenAI-compatible endpoint**: same as LM Studio —
  whatever you point `OPENAI_BASE_URL` at is the trust boundary.

## What is stored locally

The tool writes the following files in the repo's working directory:

| File | Contents | Sensitive? |
|---|---|---|
| `config/credentials.json` | OAuth client ID + secret you got from Google Cloud | yes — keep out of git |
| `config/token.json` | OAuth access + refresh token for your account | yes — full Gmail access |
| `config/labels.yaml` | Your label catalog | no |
| `config/rules.md` | Your classification rules | no |
| `config/whitelist.txt` | Sender addresses to never trash | varies |
| `state.json` | Classify resume checkpoint (processed thread IDs) | no |
| `state-applied.json` | `apply-log` resume checkpoint (applied thread IDs) | no |
| `relabel-state.json` | `relabel` resume checkpoint | no |
| `dry-run.log` | Per-email decisions from a `--dry-run` pass | yes — contains subjects + senders |
| `applied.log` | Per-email actions actually taken | yes |
| `replay-preview.log` | Output of `apply-log --dry-run` | yes |
| `relabel.log` | Output of `relabel` | yes |
| `config/backend.env` | Backend selection + API keys for cloud providers | yes — keep out of git |

The `.gitignore` excludes the sensitive ones from accidental commits.
**Never commit `credentials.json` or `token.json`** — both grant access
to your Gmail. Treat them like a password.

## Gmail permissions used

The tool requests the `gmail.modify` scope, which allows:

- Reading messages and labels
- Applying labels (including the system `TRASH` label, which moves to
  trash — recoverable for 30 days)
- Creating new labels

It does NOT request:

- `gmail.compose` (sending mail) — the tool cannot send anything
- `gmail.settings.basic` / `.sharing` — cannot modify forwarding/filters
- Permanent delete (`messages.delete` requires the broader `mail.google.com`
  scope, which we explicitly do not request)

## Audit trail

Every state-changing action is logged to `applied.log` with timestamp,
thread ID, sender, subject, action, and reason. If something goes wrong
(or just looks wrong in retrospect), you can reconstruct exactly what the
tool did and recover from Gmail's trash.
