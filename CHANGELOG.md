# Changelog

Notable changes per release. Fuller notes, with install snippets and
test counts, are on the
[GitHub releases page](https://github.com/brosequist/gmail-cleanup-agent/releases).
Versions follow [semantic versioning](https://semver.org/).

## Unreleased

### Fixed

- **A dry run no longer makes `--apply` skip threads.** `classify` and
  `relabel` used the same resume state in both modes, so `--apply` after
  a dry run silently skipped every thread the dry run had only looked at.
  Dry runs now default to their own state files (`state-dry-run.json`,
  `relabel-state-dry-run.json`); `--apply` keeps `state.json` and
  `relabel-state.json`.
- **State files record the mode that wrote them**, and an explicit
  `--state-file` from the other mode is refused with exit code 2 and the
  reason, before Gmail is touched. State files without a mode (1.4 and
  earlier) still load.
- **`--limit` counts only threads this run processes.** Threads skipped
  on resume used to count, so a resumed `--limit N` run could process
  nothing.
- **`apply-log` replays label removals.** It sends `removeLabelIds` for a
  keep row's `removed_labels` (a removal-only row still makes the call),
  skips names Gmail no longer has, and records them in the audit and
  preview logs. Replaying a dry run now matches `classify --apply`.
- **`auth` prints the token path it actually wrote**, not a fixed
  `config/token.json`.

### Added

- `gmail-cleanup --version`.

### Docs

- README: a full command reference with defaults, every environment
  variable, the log and state file formats, and how a run behaves on
  bad model output, odd threads and retries. Several statements that no
  longer matched the code were corrected.
- `docs/cost-math.md` re-measured on the 1.4 prompt with real
  tokenizers (Haiku estimate ~$69 → ~$129); `docs/oauth-setup.md` covers
  PyPI/Docker, headless `auth` and the 7-day Testing-mode token expiry.

## 1.4.0 — 2026-10-09

### Changed

- **One or two labels per kept email.** A kept email gets one category
  label by default and a second only when two categories are
  independently true of it (a hotel booking receipt is both travel and a
  receipt); never a third, and never a second label to avoid choosing.
  Same contract and wording as the companion n8n engine.
- Applies everywhere: `classify` decisions carry a `labels` array and
  `--apply` adds every label in one `threads.modify`; `apply-log` replays
  both; `relabel` compares label sets (adds new, removes dropped; a
  reorder is no change).

### Compatibility

- Logs written by 1.3 and earlier keep working in `apply-log` and
  `relabel --input-log`.
- New rows keep the old fields next to the new ones: `label` beside
  `labels`, `old_label`/`new_label` beside `old_labels`/`new_labels`.
- Trash rows carry `labels: []`.
- A model answering in the 1.3 shape is still accepted; more than two
  labels are cut to two and an unknown second label is dropped.

## 1.3.0 — 2026-05-29

### Added

- **`--remove-label NAME`** (repeatable, `classify` and `relabel`):
  strips NAME from every kept and whitelisted thread the pass touches,
  in the same `threads.modify` as the category label.
- **`removable:` in `labels.yaml`**: LLM-driven removal. The prompt gains
  a *Removable labels* section and a `Current labels:` line per thread,
  and the model may return `remove_labels`; a strip is honored only for
  a catalog label actually on the thread.
- Decision logs record removals as `removed_labels`.

## 1.2.1 — 2026-05-21

### Fixed

- `__version__` reports the real version (was hardcoded `0.1.0`); it is
  now read from the installed package metadata.

### CI

- Workflow actions bumped to their Node 24 majors.

## 1.2.0 — 2026-05-21

### Added

- **`--reviewed-label[=NAME]`**: labels every kept email (whitelisted
  included) and excludes that label from this and later runs via a
  `-label:` query filter.
- **`--skip-label NAME`** (repeatable): excludes further labels the same
  way.
- `apply-log` replays a row's `reviewed_label`.

### Fixed

- A failed Gmail mutation under `--apply` is logged as `action: "error"`
  (retryable with `--retry-errors`) instead of a `keep`/`trash` that
  claimed success.
- A `--reviewed-label` differing only in case from an existing label
  reuses it; nested `Parent/Child` names work; a name containing `"` is
  dropped from the skip filter with a warning.

## 1.1.2 — 2026-05-19

First release published to PyPI, as **`gmail-llm-cleanup`**
(`gmail-cleanup-agent` was taken by an unrelated project). The import
name `gmail_cleanup` and the `gmail-cleanup` command are unchanged. The
Docker image moved to `ghcr.io/brosequist/gmail-llm-cleanup`.

## 1.1.1 — 2026-05-19

Tag only; no GitHub release.

### Fixed

- Docker build: the wheel install passed `*.whl[claude,openai]` to pip,
  where `[…]` is a shell glob and never matched. The wheel path is now
  captured first.

## 1.1.0 — 2026-05-19

Tagged before the PyPI rename; install 1.1.2 instead.

### Added

- Runtime path resolution: config from `$GMAIL_CLEANUP_CONFIG_DIR`, else
  `./config`; state and logs in `$GMAIL_CLEANUP_WORK_DIR`, else the
  current directory. A PyPI install works from any directory.
- Multi-arch (amd64 + arm64) Docker image with both extras and the
  `/config` + `/work` mount convention.
- PyPI publishing via Trusted Publishers; GHCR publishing on tag push.

### Fixed

- `apply-log` gives a clear error when `--credentials` is missing.

## 1.0.0 — 2026-05-19

First stable release, validated on a 312,262-thread mailbox (83 %
trashed, 0 final errors; see [docs/results.md](docs/results.md)).

- Subcommands `classify`, `apply-log`, `relabel` and `auth`.
- Backends: Ollama (default), OpenAI-compatible (LM Studio, llama.cpp,
  vLLM, OpenAI) and Anthropic Claude, selected with `GCA_BACKEND`.
- Dry run by default, trash rather than delete, a sender whitelist the
  model cannot override, and the narrow `gmail.modify` scope.
- Resume checkpoints, `--retry-errors`, retries on transient Gmail and
  network errors, `--include-body`, `--console-log`, and the
  `PRE_RUN_COMMAND` hook for port-forwards and tunnels.
- Age in days and `List-Unsubscribe` presence sent to the model.
