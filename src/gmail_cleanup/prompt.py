"""Prompt builder. Translates the user's labels.yaml + rules.md +
whitelist into a structured LLM prompt and validates responses."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class LabelCatalog:
    existing: list[str] = field(default_factory=list)
    auto_create: dict[str, str] = field(default_factory=dict)  # name → description

    @property
    def all_keep_labels(self) -> list[str]:
        return list(self.existing) + list(self.auto_create.keys())

    @classmethod
    def load(cls, path: Path) -> "LabelCatalog":
        d = yaml.safe_load(path.read_text())
        return cls(
            existing=list(d.get("existing", []) or []),
            auto_create=dict(d.get("auto_create", {}) or {}),
        )


def load_whitelist(path: Path) -> list[str]:
    """Return the non-comment, non-blank lines of the whitelist."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s.lower())
    return out


def is_whitelisted(sender: str, whitelist: list[str]) -> bool:
    s = sender.lower()
    for entry in whitelist:
        if entry.startswith("@"):
            if entry in s:
                return True
        elif "@" in entry:
            # exact-ish match: substring match on the address
            if entry in s:
                return True
        else:
            if entry in s:
                return True
    return False


def build_prompt(rules_md: str, catalog: LabelCatalog, batch: list[dict]) -> str:
    """Render the system+user prompt as a single string. The structure:

      <rules_md>

      # Available labels

      <label catalog with descriptions>

      # Output format

      Return ONLY a single JSON object with key `decisions`, an array of
      one object per input email in the same order, each:
        {"id": "<id>", "action": "keep"|"trash", "label": "<label or null>"}

      # Emails

      [25 numbered entries]
    """
    label_lines = []
    for name in catalog.existing:
        label_lines.append(f"- `{name}` (existing)")
    for name, desc in catalog.auto_create.items():
        label_lines.append(f"- `{name}` — {desc}")

    emails_block = []
    for i, e in enumerate(batch, 1):
        emails_block.append(
            f"## {i}. id: {e['id']}\n"
            f"From: {e['sender']}\n"
            f"Subject: {e['subject']}\n"
            f"Snippet: {e['snippet'][:300]}\n"
        )

    return f"""{rules_md.strip()}

# Available labels

When `action` is `keep`, choose the single best-matching label from this
list. If `action` is `trash`, set `label` to `null`. Pick exactly one
label per kept email — no nesting, no comma-separated values.

{chr(10).join(label_lines)}

# Output format

Return ONLY a JSON object with this exact structure (no prose, no markdown):

```json
{{"decisions": [
  {{"id": "...", "action": "keep", "label": "Receipts"}},
  {{"id": "...", "action": "trash", "label": null}}
]}}
```

The `decisions` array must have exactly the same number of entries as
input emails, in the same order. Each `id` must match an input id. Each
`action` is either `"keep"` or `"trash"`. Each `label` is either one of
the labels above (when keeping) or `null` (when trashing).

# Emails to classify

{chr(10).join(emails_block)}
"""


# Strict regex for one decision entry — used to repair partial output
# when a model occasionally truncates JSON.
_DECISION_RE = re.compile(
    r'\{\s*"id"\s*:\s*"(?P<id>[^"]+)"\s*,'
    r'\s*"action"\s*:\s*"(?P<action>keep|trash)"\s*,'
    r'\s*"label"\s*:\s*(?:"(?P<label>[^"]*)"|null)\s*\}',
    re.DOTALL,
)


def parse_decisions(raw: str) -> list[dict]:
    """Parse the LLM response. Tries strict JSON first; falls back to
    regex extraction if the model wraps in prose / markdown fences."""
    import json

    # Strip common wrappers
    s = raw.strip()
    for fence in ("```json", "```"):
        if s.startswith(fence):
            s = s[len(fence):].lstrip()
        if s.endswith("```"):
            s = s[:-3].rstrip()

    # Try direct JSON
    try:
        d = json.loads(s)
        if isinstance(d, dict) and isinstance(d.get("decisions"), list):
            return d["decisions"]
    except json.JSONDecodeError:
        pass

    # Fallback: regex-pick decisions out of whatever the model returned
    out = []
    for m in _DECISION_RE.finditer(raw):
        out.append({
            "id": m.group("id"),
            "action": m.group("action"),
            "label": m.group("label") if m.group("label") is not None else None,
        })
    return out


def validate_decisions(
    decisions: list[dict], batch: list[dict], catalog: LabelCatalog
) -> tuple[list[dict], list[str]]:
    """Return (valid_decisions, errors). Each valid decision is keyed to
    an input by id and has been sanity-checked against the catalog. Any
    inputs the LLM didn't classify get DEFAULTED to keep-no-label —
    safe failure mode (preserves the email)."""
    out, missing_ids, errors = validate_decisions_strict(decisions, batch, catalog)
    for eid in missing_ids:
        errors.append(f"id={eid}: missing decision (defaulted to keep)")
        out.append({"id": eid, "action": "keep", "label": None})
    return out, errors


def validate_decisions_strict(
    decisions: list[dict], batch: list[dict], catalog: LabelCatalog
) -> tuple[list[dict], set[str], list[str]]:
    """Strict validator used for retry-on-missing. Returns:
      (good_decisions, missing_ids, errors)
    `missing_ids` is the set of input ids the LLM didn't decide on (or
    decided invalidly) — caller can re-prompt with just those to recover
    from the LLM-skipped-some-items failure mode.
    """
    valid_labels = set(catalog.all_keep_labels)
    by_id = {e["id"]: e for e in batch}
    seen_ids: set[str] = set()
    out: list[dict] = []
    errors: list[str] = []
    for d in decisions:
        eid = d.get("id")
        if eid not in by_id:
            errors.append(f"unknown id: {eid!r}")
            continue
        if eid in seen_ids:
            errors.append(f"duplicate id: {eid!r}")
            continue
        action = d.get("action")
        if action not in ("keep", "trash"):
            errors.append(f"id={eid}: bad action {action!r}")
            continue
        label = d.get("label")
        if action == "keep" and label not in valid_labels:
            errors.append(f"id={eid}: unknown label {label!r}")
            continue
        if action == "trash":
            label = None
        seen_ids.add(eid)
        out.append({"id": eid, "action": action, "label": label})
    missing_ids = set(by_id.keys()) - seen_ids
    return out, missing_ids, errors


# ---------------- relabel-only pass ----------------
#
# Used by the `relabel` subcommand. Unlike classify, this NEVER decides
# keep-vs-trash — the emails are all already-kept. The model's only job
# is to pick the best-fitting label from the (possibly expanded)
# catalog. This keeps the reorganization safe: nothing can be sent to
# trash by a relabel pass.


def build_relabel_prompt(catalog: LabelCatalog, batch: list[dict]) -> str:
    """Render a label-only prompt. Each batch item is a dict with keys
    `id`, `sender`, `subject`, and optionally `snippet` and
    `current_label`. The model returns one label per email."""
    label_lines = []
    for name in catalog.existing:
        label_lines.append(f"- `{name}` (existing)")
    for name, desc in catalog.auto_create.items():
        label_lines.append(f"- `{name}` — {desc}")

    emails_block = []
    for i, e in enumerate(batch, 1):
        lines = [f"## {i}. id: {e['id']}", f"From: {e['sender']}", f"Subject: {e['subject']}"]
        if e.get("snippet"):
            lines.append(f"Snippet: {e['snippet'][:300]}")
        if e.get("current_label"):
            lines.append(f"Current label: {e['current_label']}")
        emails_block.append("\n".join(lines) + "\n")

    return f"""You are an email-organizing assistant. Every email below has
already been reviewed and is being KEPT — you are NOT deciding whether to
keep or trash anything. Your only task is to assign each email the single
best-fitting label from the catalog.

# Available labels

Choose exactly one label per email — the closest fit. If the email's
`Current label` is still the best fit, return that same label. Only
choose a different label when another one clearly fits better (for
example, a newly added category that is a tighter match).

{chr(10).join(label_lines)}

# Output format

Return ONLY a JSON object with this exact structure (no prose, no markdown):

```json
{{"decisions": [
  {{"id": "...", "label": "Receipts"}},
  {{"id": "...", "label": "Travel"}}
]}}
```

The `decisions` array must have exactly the same number of entries as
input emails, in the same order. Each `id` must match an input id. Each
`label` must be exactly one of the labels listed above.

# Emails to label

{chr(10).join(emails_block)}
"""


_RELABEL_RE = re.compile(
    r'\{\s*"id"\s*:\s*"(?P<id>[^"]+)"\s*,'
    r'\s*"label"\s*:\s*"(?P<label>[^"]*)"\s*\}',
    re.DOTALL,
)


def parse_relabel_decisions(raw: str) -> list[dict]:
    """Parse a relabel response. Strict JSON first, regex fallback."""
    import json

    s = raw.strip()
    for fence in ("```json", "```"):
        if s.startswith(fence):
            s = s[len(fence):].lstrip()
        if s.endswith("```"):
            s = s[:-3].rstrip()
    try:
        d = json.loads(s)
        if isinstance(d, dict) and isinstance(d.get("decisions"), list):
            return d["decisions"]
    except json.JSONDecodeError:
        pass

    out = []
    for m in _RELABEL_RE.finditer(raw):
        out.append({"id": m.group("id"), "label": m.group("label")})
    return out


def validate_relabel_decisions(
    decisions: list[dict], batch: list[dict], catalog: LabelCatalog
) -> tuple[list[dict], set[str], list[str]]:
    """Strict relabel validator. Returns (good, missing_ids, errors).
    `good` entries are {id, label} with label guaranteed to be in the
    catalog. Caller re-prompts missing ids, then falls back to keeping
    each missing email's existing label (never drops a label)."""
    valid_labels = set(catalog.all_keep_labels)
    by_id = {e["id"]: e for e in batch}
    seen_ids: set[str] = set()
    out: list[dict] = []
    errors: list[str] = []
    for d in decisions:
        eid = d.get("id")
        if eid not in by_id:
            errors.append(f"unknown id: {eid!r}")
            continue
        if eid in seen_ids:
            errors.append(f"duplicate id: {eid!r}")
            continue
        label = d.get("label")
        if label not in valid_labels:
            errors.append(f"id={eid}: unknown label {label!r}")
            continue
        seen_ids.add(eid)
        out.append({"id": eid, "label": label})
    missing_ids = set(by_id.keys()) - seen_ids
    return out, missing_ids, errors
