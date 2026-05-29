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
    # Labels the LLM may *propose* removing from a thread, with a short
    # description of when they should be stripped. Independent of the
    # keep-label catalog: a label here is one the user has retired or
    # wants the model to retire opportunistically. Validation also
    # requires the label to actually be on the thread.
    removable: dict[str, str] = field(default_factory=dict)

    @property
    def all_keep_labels(self) -> list[str]:
        return list(self.existing) + list(self.auto_create.keys())

    @classmethod
    def load(cls, path: Path) -> "LabelCatalog":
        d = yaml.safe_load(path.read_text())
        return cls(
            existing=list(d.get("existing", []) or []),
            auto_create=dict(d.get("auto_create", {}) or {}),
            removable=dict(d.get("removable", {}) or {}),
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

      # Removable labels  (only when catalog.removable is non-empty)

      <removable catalog with descriptions + opt-in remove_labels field>

      # Output format

      Return ONLY a single JSON object with key `decisions`, an array of
      one object per input email in the same order, each:
        {"id": "<id>", "action": "keep"|"trash", "label": "<label or null>",
         "remove_labels": ["X", "Y"]}  # optional; only when removable catalog is on

      # Emails

      [25 numbered entries — `Current labels:` line on each is included
       when a thread actually carries any user-defined labels]
    """
    label_lines = []
    for name in catalog.existing:
        label_lines.append(f"- `{name}` (existing)")
    for name, desc in catalog.auto_create.items():
        label_lines.append(f"- `{name}` — {desc}")

    removable_section = _build_removable_section(catalog)

    emails_block = []
    for i, e in enumerate(batch, 1):
        # Optional metadata signals; only render when present so older
        # callers and saved batches stay compatible.
        meta_lines = []
        age = e.get("age_days")
        if age is not None:
            meta_lines.append(f"Age: {age} days")
        if e.get("has_list_unsubscribe"):
            meta_lines.append("List-Unsubscribe: yes")
        # Render current Gmail labels only when there are any AND the
        # removable feature is in use — otherwise the line is noise.
        if catalog.removable and e.get("current_labels"):
            meta_lines.append("Current labels: " + ", ".join(e["current_labels"]))
        meta_str = "".join(f"{m}\n" for m in meta_lines)
        # Body is only included when --include-body was passed to classify;
        # otherwise the field is absent or empty, and we fall back to the
        # snippet alone (the common path for bulk inbox-bankruptcy runs).
        body = e.get("body") or ""
        body_section = f"Body:\n{body}\n" if body else ""
        emails_block.append(
            f"## {i}. id: {e['id']}\n"
            f"From: {e['sender']}\n"
            f"Subject: {e['subject']}\n"
            f"{meta_str}"
            f"Snippet: {e['snippet'][:300]}\n"
            f"{body_section}"
        )

    schema_example = (
        '{"id": "...", "action": "keep", "label": "Receipts"'
        + (', "remove_labels": ["LegacyTag"]' if catalog.removable else '')
        + '},\n  {"id": "...", "action": "trash", "label": null}'
    )

    return f"""{rules_md.strip()}

# Available labels

When `action` is `keep`, choose the single best-matching label from this
list. If `action` is `trash`, set `label` to `null`. Pick exactly one
label per kept email — no nesting, no comma-separated values.

{chr(10).join(label_lines)}
{removable_section}
# Output format

Return ONLY a JSON object with this exact structure (no prose, no markdown):

```json
{{"decisions": [
  {schema_example}
]}}
```

The `decisions` array must have exactly the same number of entries as
input emails, in the same order. Each `id` must match an input id. Each
`action` is either `"keep"` or `"trash"`. Each `label` is either one of
the labels above (when keeping) or `null` (when trashing).

# Emails to classify

{chr(10).join(emails_block)}
"""


def _build_removable_section(catalog: LabelCatalog) -> str:
    """The 'Removable labels' chunk of the prompt. Empty string when the
    feature is off so the rest of the prompt stays unchanged byte-for-byte
    with the pre-feature baseline. Used by both build_prompt and
    build_relabel_prompt."""
    if not catalog.removable:
        return ""
    lines = [f"- `{name}` — {desc}" for name, desc in catalog.removable.items()]
    return f"""
# Removable labels (optional)

You MAY propose stripping any of these labels from an email by listing
them in an OPTIONAL `remove_labels` array on the email's decision. Only
list a label here when ALL of these are true:
  1. The label appears in this catalog.
  2. The label is in the email's `Current labels` line.
  3. The label no longer fits the email (per its description below).

If you have no strong reason, omit `remove_labels` entirely. Do NOT
list category/keep labels here — only labels from this catalog.

{chr(10).join(lines)}
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
    regex extraction if the model wraps in prose / markdown fences.

    Optional `remove_labels` (list of strings) is preserved on JSON
    parses; the regex fallback drops it (recovery path is best-effort).
    """
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

    Optional `remove_labels` on a decision is validated against the
    catalog's `removable` set AND the batch item's `current_labels`.
    Invalid entries are dropped with a warning; the decision is still
    accepted (label removal is opt-in, not a correctness contract).
    `remove_labels` is silently dropped on trash decisions.
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
        remove_labels = _filter_remove_labels(
            d.get("remove_labels"), by_id[eid], catalog,
            action, eid, errors)
        seen_ids.add(eid)
        decision: dict = {"id": eid, "action": action, "label": label}
        if remove_labels:
            decision["remove_labels"] = remove_labels
        out.append(decision)
    missing_ids = set(by_id.keys()) - seen_ids
    return out, missing_ids, errors


def _filter_remove_labels(
    proposed, batch_item: dict, catalog: LabelCatalog,
    action: str, eid: str, errors: list[str],
) -> list[str]:
    """Filter a model-proposed remove_labels array down to the entries
    that are (a) listed in catalog.removable and (b) actually on the
    thread per batch_item['current_labels']. Trash decisions get a
    silent skip — Gmail's trash already hides labels. Returns the
    cleaned list (possibly empty)."""
    if not proposed or not isinstance(proposed, list):
        return []
    if action == "trash":
        return []
    catalog_set = set(catalog.removable.keys())
    on_thread = set(batch_item.get("current_labels") or [])
    out: list[str] = []
    seen: set[str] = set()
    for name in proposed:
        if not isinstance(name, str) or name in seen:
            continue
        seen.add(name)
        if name not in catalog_set:
            errors.append(
                f"id={eid}: remove_label {name!r} not in removable catalog")
            continue
        if name not in on_thread:
            # Not an error — model may not see the freshest label state.
            # Drop silently to keep prompt noise down.
            continue
        out.append(name)
    return out


# ---------------- relabel-only pass ----------------
#
# Used by the `relabel` subcommand. Unlike classify, this NEVER decides
# keep-vs-trash — the emails are all already-kept. The model's only job
# is to pick the best-fitting label from the (possibly expanded)
# catalog. This keeps the reorganization safe: nothing can be sent to
# trash by a relabel pass.


def build_relabel_prompt(catalog: LabelCatalog, batch: list[dict]) -> str:
    """Render a label-only prompt. Each batch item is a dict with keys
    `id`, `sender`, `subject`, and optionally `snippet`,
    `current_label`, and `current_labels` (all Gmail labels on the
    thread, used only when the removable catalog is on). The model
    returns one label per email plus an optional `remove_labels`."""
    label_lines = []
    for name in catalog.existing:
        label_lines.append(f"- `{name}` (existing)")
    for name, desc in catalog.auto_create.items():
        label_lines.append(f"- `{name}` — {desc}")

    removable_section = _build_removable_section(catalog)

    emails_block = []
    for i, e in enumerate(batch, 1):
        lines = [f"## {i}. id: {e['id']}", f"From: {e['sender']}", f"Subject: {e['subject']}"]
        if e.get("snippet"):
            lines.append(f"Snippet: {e['snippet'][:300]}")
        if e.get("current_label"):
            lines.append(f"Current label: {e['current_label']}")
        if catalog.removable and e.get("current_labels"):
            lines.append("Current labels: " + ", ".join(e["current_labels"]))
        emails_block.append("\n".join(lines) + "\n")

    schema_example = (
        '{"id": "...", "label": "Receipts"'
        + (', "remove_labels": ["LegacyTag"]' if catalog.removable else '')
        + '},\n  {"id": "...", "label": "Travel"}'
    )

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
{removable_section}
# Output format

Return ONLY a JSON object with this exact structure (no prose, no markdown):

```json
{{"decisions": [
  {schema_example}
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
    `good` entries are {id, label} (with optional `remove_labels`) and
    label is guaranteed to be in the catalog. Caller re-prompts missing
    ids, then falls back to keeping each missing email's existing label
    (never drops a label).

    Optional `remove_labels` validated the same way as in classify:
    must be in `catalog.removable` AND in the batch item's
    `current_labels`. Invalid entries dropped.
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
        label = d.get("label")
        if label not in valid_labels:
            errors.append(f"id={eid}: unknown label {label!r}")
            continue
        remove_labels = _filter_remove_labels(
            d.get("remove_labels"), by_id[eid], catalog,
            "keep", eid, errors)
        seen_ids.add(eid)
        entry: dict = {"id": eid, "label": label}
        if remove_labels:
            entry["remove_labels"] = remove_labels
        out.append(entry)
    missing_ids = set(by_id.keys()) - seen_ids
    return out, missing_ids, errors
