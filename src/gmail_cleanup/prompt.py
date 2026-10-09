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


# A kept email gets one category label by default and a second only when
# two categories are independently true of it. Same contract as the n8n
# engine that shares this repo's rules.md and labels.yaml.
MAX_LABELS = 2


def decision_labels(d: dict) -> list[str]:
    """Category labels on a decision or decision-log record, in order.

    Reads the `labels` array (1.4+), falling back to the scalar `label`
    that v1.3.0 and earlier wrote, so old logs keep working everywhere a
    decision is read. Blank and duplicate names are dropped; never None.
    A record that has a `labels` key is trusted over its `label` key.
    """
    raw = d.get("labels")
    if isinstance(raw, list):
        names = raw
    else:
        legacy = d.get("label")
        names = [legacy] if legacy else []
    out: list[str] = []
    for name in names:
        if isinstance(name, str) and name and name not in out:
            out.append(name)
    return out


def with_labels(d: dict, labels: list[str]) -> dict:
    """Set a decision's labels: the `labels` array plus `label`, its first
    entry, kept as a legacy mirror for readers written against v1.3.0."""
    d["labels"] = list(labels)
    d["label"] = labels[0] if labels else None
    return d


def _validate_labels(
    d: dict, valid_labels: set[str], eid: str, errors: list[str],
) -> list[str] | None:
    """Check a keep decision's labels against the catalog. Returns the
    accepted labels, or None when nothing usable is left (the caller
    treats that as an invalid decision and re-prompts). More than
    MAX_LABELS are cut to the first MAX_LABELS, and unknown names are
    dropped while at least one known one remains, both with an error
    event, as the n8n engine does."""
    labels = decision_labels(d)
    if len(labels) > MAX_LABELS:
        errors.append(f"id={eid}: {len(labels)} labels, kept first "
                      f"{MAX_LABELS}: {'/'.join(labels)}")
        labels = labels[:MAX_LABELS]
    unknown = [n for n in labels if n not in valid_labels]
    known = [n for n in labels if n in valid_labels]
    if not known:
        # Same message shape as v1.3.0 for the one-label and no-label cases.
        if len(unknown) == 1:
            shown = unknown[0]
        elif unknown:
            shown = unknown
        else:
            shown = d.get("labels", d.get("label"))
        errors.append(f"id={eid}: unknown label {shown!r}")
        return None
    if unknown:
        errors.append(f"id={eid}: dropped unknown label(s) {unknown!r}")
    return known


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


# The one-or-two-label contract, worded as in the n8n engine's prompt so a
# rule in the shared rules.md means the same thing to both. Used by
# build_prompt and build_relabel_prompt.
_TWO_LABEL_GUIDANCE = """**One label is the default.** Add a SECOND label only when two categories
are independently true of the same email: a hotel booking receipt really
is both a travel record and a receipt; a vet bill really is both a pet
matter and a statement. Never a third.

**Do not use a second label to avoid choosing.** Where the rules already
settle a pairing (a bill goes in one category, a one-off purchase in
another), that decision is made: apply the one the rules name. A second
label is for genuine overlap, not for hedging.
"""


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
        {"id": "<id>", "action": "keep"|"trash", "labels": ["<one or two>"],
         "remove_labels": ["X", "Y"]}  # optional; only when removable catalog is on
      `labels` is [] on trash: unlike the n8n engine, which defers trash
      for 30 days and so categorises it, this agent trashes immediately.

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
        '{"id": "...", "action": "keep", "labels": ["Receipts"]'
        + (', "remove_labels": ["LegacyTag"]' if catalog.removable else '')
        + '},\n  {"id": "...", "action": "keep", "labels": ["Travel", "Receipts"]},'
        + '\n  {"id": "...", "action": "trash", "labels": []}'
    )

    return f"""{rules_md.strip()}

# Available labels

When `action` is `keep`, give the email its best-matching label from this
list in a `labels` array. Use label names exactly as written: no nesting,
no comma-separated values, no invented names. If `action` is `trash`, set
`labels` to an empty array `[]`.

{_TWO_LABEL_GUIDANCE}
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
`action` is either `"keep"` or `"trash"`. Each `labels` is an array of one
or two of the labels above when keeping, and `[]` when trashing.

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


# The label part of a decision in the regex fallback: the 1.4 `labels`
# array, or the scalar `label` an older prompt or model may still emit.
_LABELS_FRAGMENT = (
    r'(?:"labels"\s*:\s*\[(?P<labels>[^\]]*)\]'
    r'|"label"\s*:\s*(?:"(?P<label>[^"]*)"|null))'
)

# Strict regex for one decision entry — used to repair partial output
# when a model occasionally truncates JSON.
_DECISION_RE = re.compile(
    r'\{\s*"id"\s*:\s*"(?P<id>[^"]+)"\s*,'
    r'\s*"action"\s*:\s*"(?P<action>keep|trash)"\s*,'
    r'\s*' + _LABELS_FRAGMENT + r'\s*\}',
    re.DOTALL,
)


def _regex_labels(m: re.Match) -> list[str]:
    """Labels from a regex-fallback match, whichever shape it used."""
    if m.group("labels") is not None:
        return re.findall(r'"([^"]*)"', m.group("labels"))
    return [m.group("label")] if m.group("label") else []


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
            "labels": _regex_labels(m),
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
        out.append(with_labels({"id": eid, "action": "keep"}, []))
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
        if action == "keep":
            labels = _validate_labels(d, valid_labels, eid, errors)
            if labels is None:
                continue
        else:
            # Trash takes no category here (it is gone at once); anything
            # the model sent is ignored rather than treated as an error.
            labels = []
        remove_labels = _filter_remove_labels(
            d.get("remove_labels"), by_id[eid], catalog,
            action, eid, errors)
        seen_ids.add(eid)
        decision: dict = with_labels({"id": eid, "action": action}, labels)
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
    `id`, `sender`, `subject`, and optionally `snippet`, the email's
    current category label(s) as `current_category_labels` (list) or the
    pre-1.4 `current_label` (str), and `current_labels` (all Gmail labels
    on the thread, used only when the removable catalog is on). The model
    returns one or two labels per email plus an optional `remove_labels`."""
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
        current = e.get("current_category_labels")
        if current is None and e.get("current_label"):
            current = [e["current_label"]]
        if current:
            lines.append(f"Current label: {', '.join(current)}")
        if catalog.removable and e.get("current_labels"):
            lines.append("Current labels: " + ", ".join(e["current_labels"]))
        emails_block.append("\n".join(lines) + "\n")

    schema_example = (
        '{"id": "...", "labels": ["Receipts"]'
        + (', "remove_labels": ["LegacyTag"]' if catalog.removable else '')
        + '},\n  {"id": "...", "labels": ["Travel", "Receipts"]}'
    )

    return f"""You are an email-organizing assistant. Every email below has
already been reviewed and is being KEPT — you are NOT deciding whether to
keep or trash anything. Your only task is to assign each email the
best-fitting label(s) from the catalog.

# Available labels

Give each email the closest-fitting label in a `labels` array. If the
email's `Current label` is still the best fit, return that same label (or
both, if it has two). Only choose a different label when another one
clearly fits better (for example, a newly added category that is a tighter
match).

{_TWO_LABEL_GUIDANCE}
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
`labels` must be an array of one or two of the labels listed above.

# Emails to label

{chr(10).join(emails_block)}
"""


_RELABEL_RE = re.compile(
    r'\{\s*"id"\s*:\s*"(?P<id>[^"]+)"\s*,'
    r'\s*' + _LABELS_FRAGMENT + r'\s*\}',
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
        out.append({"id": m.group("id"), "labels": _regex_labels(m)})
    return out


def validate_relabel_decisions(
    decisions: list[dict], batch: list[dict], catalog: LabelCatalog
) -> tuple[list[dict], set[str], list[str]]:
    """Strict relabel validator. Returns (good, missing_ids, errors).
    `good` entries are {id, labels, label} (with optional `remove_labels`):
    one or two labels, all in the catalog, with `label` mirroring the
    first. Caller re-prompts missing ids, then falls back to keeping each
    missing email's existing labels (never drops a label).

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
        labels = _validate_labels(d, valid_labels, eid, errors)
        if labels is None:
            continue
        remove_labels = _filter_remove_labels(
            d.get("remove_labels"), by_id[eid], catalog,
            "keep", eid, errors)
        seen_ids.add(eid)
        entry: dict = with_labels({"id": eid}, labels)
        if remove_labels:
            entry["remove_labels"] = remove_labels
        out.append(entry)
    missing_ids = set(by_id.keys()) - seen_ids
    return out, missing_ids, errors
