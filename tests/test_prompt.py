"""Unit tests for prompt building, parsing, validation, and whitelist."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gmail_cleanup.prompt import (
    LabelCatalog,
    build_prompt,
    build_relabel_prompt,
    is_whitelisted,
    load_whitelist,
    parse_decisions,
    parse_relabel_decisions,
    validate_decisions,
    validate_decisions_strict,
    validate_relabel_decisions,
)


# ---------------- LabelCatalog ----------------


def test_label_catalog_load(tmp_path: Path):
    p = tmp_path / "labels.yaml"
    p.write_text(
        "existing:\n  - Filed\n  - Taxes\n"
        "auto_create:\n  Receipts: orders\n  Family: personal\n"
    )
    cat = LabelCatalog.load(p)
    assert cat.existing == ["Filed", "Taxes"]
    assert cat.auto_create == {"Receipts": "orders", "Family": "personal"}
    assert set(cat.all_keep_labels) == {"Filed", "Taxes", "Receipts", "Family"}


def test_label_catalog_empty_sections(tmp_path: Path):
    p = tmp_path / "labels.yaml"
    p.write_text("existing:\nauto_create:\n")
    cat = LabelCatalog.load(p)
    assert cat.existing == []
    assert cat.auto_create == {}


# ---------------- whitelist ----------------


def test_load_whitelist_strips_comments_and_blanks(tmp_path: Path):
    p = tmp_path / "wl.txt"
    p.write_text("\n# top comment\n@mydomain.com\n\nMOM@example.com\n# trailing\n")
    wl = load_whitelist(p)
    # Lowercased, comments + blanks removed
    assert wl == ["@mydomain.com", "mom@example.com"]


def test_load_whitelist_missing_file(tmp_path: Path):
    assert load_whitelist(tmp_path / "absent.txt") == []


@pytest.mark.parametrize("sender,patterns,expected", [
    ("alice@mydomain.com", ["@mydomain.com"], True),
    ("Alice <alice@MyDomain.com>", ["@mydomain.com"], True),  # case-insensitive
    ("bob@other.com", ["@mydomain.com"], False),
    ("school@school.edu", ["school.edu"], True),  # bare-domain match
    ("mom@example.com", ["mom@example.com"], True),
    ("dad@example.com", ["mom@example.com"], False),
    ("alice@mydomain.com", [], False),  # empty whitelist never matches
])
def test_is_whitelisted(sender, patterns, expected):
    assert is_whitelisted(sender, patterns) is expected


# ---------------- build_prompt ----------------


@pytest.fixture
def catalog():
    return LabelCatalog(existing=["Filed"], auto_create={"Receipts": "orders"})


def test_build_prompt_includes_rules_labels_and_emails(catalog):
    out = build_prompt(
        "ALWAYS KEEP family",
        catalog,
        [{"id": "abc", "sender": "a@b.com", "subject": "hi", "snippet": "x"}],
    )
    assert "ALWAYS KEEP family" in out
    assert "Filed" in out and "Receipts" in out
    assert "id: abc" in out
    assert "a@b.com" in out
    # JSON format instruction is the contract with the model
    assert '"decisions"' in out


def test_build_prompt_renders_age_and_list_unsubscribe(catalog):
    out = build_prompt(
        "rules", catalog,
        [{"id": "x", "sender": "s", "subject": "j", "snippet": "k",
          "age_days": 365, "has_list_unsubscribe": True}],
    )
    assert "Age: 365 days" in out
    assert "List-Unsubscribe: yes" in out


def test_build_prompt_omits_age_when_none(catalog):
    out = build_prompt(
        "rules", catalog,
        [{"id": "x", "sender": "s", "subject": "j", "snippet": "k"}],
    )
    assert "Age:" not in out
    assert "List-Unsubscribe:" not in out


def test_build_prompt_includes_body_when_present(catalog):
    out = build_prompt(
        "rules", catalog,
        [{"id": "x", "sender": "s", "subject": "j", "snippet": "k",
          "body": "FULL MESSAGE BODY"}],
    )
    assert "FULL MESSAGE BODY" in out
    assert "Body:" in out


def test_build_prompt_skips_empty_body(catalog):
    out = build_prompt(
        "rules", catalog,
        [{"id": "x", "sender": "s", "subject": "j", "snippet": "k", "body": ""}],
    )
    assert "Body:" not in out


def test_build_prompt_truncates_snippet(catalog):
    long_snip = "X" * 500
    out = build_prompt(
        "rules", catalog,
        [{"id": "x", "sender": "s", "subject": "j", "snippet": long_snip}],
    )
    # Truncated to 300 chars by the prompt builder.
    assert "X" * 300 in out
    assert "X" * 301 not in out


# ---------------- parse_decisions ----------------


def test_parse_decisions_clean_json():
    raw = json.dumps({"decisions": [
        {"id": "a", "action": "keep", "label": "Receipts"},
        {"id": "b", "action": "trash", "label": None},
    ]})
    out = parse_decisions(raw)
    assert len(out) == 2
    assert out[0]["action"] == "keep"
    assert out[1]["label"] is None


def test_parse_decisions_strips_markdown_fence():
    raw = '```json\n{"decisions":[{"id":"a","action":"keep","label":"Receipts"}]}\n```'
    out = parse_decisions(raw)
    assert out == [{"id": "a", "action": "keep", "label": "Receipts"}]


def test_parse_decisions_regex_fallback_on_prose():
    raw = (
        "Sure thing! Here are the decisions:\n"
        '{"id": "a", "action": "keep", "label": "Receipts"}\n'
        '{"id": "b", "action": "trash", "label": null}\n'
        "Hope that helps!"
    )
    out = parse_decisions(raw)
    assert {d["id"] for d in out} == {"a", "b"}


# ---------------- validate_decisions ----------------


def test_validate_decisions_strict_flags_unknown_and_duplicate(catalog):
    batch = [{"id": "a"}, {"id": "b"}]
    decs = [
        {"id": "a", "action": "keep", "label": "Receipts"},
        {"id": "a", "action": "trash", "label": None},  # duplicate
        {"id": "zzz", "action": "keep", "label": "Receipts"},  # unknown
        {"id": "b", "action": "keep", "label": "Notalabel"},  # bad label
    ]
    good, missing, errs = validate_decisions_strict(decs, batch, catalog)
    assert [g["id"] for g in good] == ["a"]
    assert missing == {"b"}
    assert any("duplicate" in e for e in errs)
    assert any("unknown id" in e for e in errs)
    assert any("unknown label" in e for e in errs)


def test_validate_decisions_strict_trash_nulls_label(catalog):
    batch = [{"id": "a"}]
    # Model returned a stray label on a trash decision — should be nulled out.
    decs = [{"id": "a", "action": "trash", "label": "Receipts"}]
    good, _, _ = validate_decisions_strict(decs, batch, catalog)
    assert good == [{"id": "a", "action": "trash", "label": None}]


def test_validate_decisions_lenient_defaults_missing_to_keep(catalog):
    batch = [{"id": "a"}, {"id": "b"}]
    decs = [{"id": "a", "action": "keep", "label": "Receipts"}]
    out, errs = validate_decisions(decs, batch, catalog)
    by_id = {d["id"]: d for d in out}
    assert by_id["b"] == {"id": "b", "action": "keep", "label": None}
    assert any("defaulted to keep" in e for e in errs)


# ---------------- relabel ----------------


def test_build_relabel_prompt_lists_current_label(catalog):
    out = build_relabel_prompt(catalog, [
        {"id": "x", "sender": "s", "subject": "j", "current_label": "Filed"},
    ])
    assert "Current label: Filed" in out
    # Critical: the JSON schema must NOT have an "action" field — the
    # whole point of relabel is that it can't ever trash an email. The
    # prompt text DOES mention trash (as part of the "you are NOT
    # deciding whether to trash" instruction), but the response schema
    # gives the model no slot to emit a trash decision.
    assert '"action"' not in out


def test_parse_relabel_decisions_clean_and_fence():
    clean = '{"decisions":[{"id":"x","label":"Receipts"}]}'
    fenced = f"```json\n{clean}\n```"
    assert parse_relabel_decisions(clean) == [{"id": "x", "label": "Receipts"}]
    assert parse_relabel_decisions(fenced) == [{"id": "x", "label": "Receipts"}]


def test_validate_relabel_decisions_rejects_unknown_label(catalog):
    batch = [{"id": "x"}]
    out, missing, errs = validate_relabel_decisions(
        [{"id": "x", "label": "Notalabel"}], batch, catalog,
    )
    assert out == []
    assert missing == {"x"}
    assert any("unknown label" in e for e in errs)


# ---------------- removable: catalog + remove_labels output ----------------


@pytest.fixture
def removable_catalog():
    return LabelCatalog(
        existing=["Filed"],
        auto_create={"Receipts": "orders"},
        removable={"OldNewsletters": "newsletters from a vendor we no longer use",
                   "LegacyAuto": "auto-tag we are retiring"},
    )


def test_label_catalog_load_reads_removable(tmp_path: Path):
    p = tmp_path / "labels.yaml"
    p.write_text(
        "existing:\n  - Filed\n"
        "auto_create:\n  Receipts: orders\n"
        "removable:\n  OldNewsletters: stale vendor tag\n"
    )
    cat = LabelCatalog.load(p)
    assert cat.removable == {"OldNewsletters": "stale vendor tag"}


def test_label_catalog_load_missing_removable_is_empty(tmp_path: Path):
    """Files predating the feature have no `removable:` key — it
    should default to an empty dict, not raise."""
    p = tmp_path / "labels.yaml"
    p.write_text("existing:\n  - Filed\nauto_create:\n  Receipts: orders\n")
    cat = LabelCatalog.load(p)
    assert cat.removable == {}


def test_build_prompt_no_removable_section_when_catalog_empty(catalog):
    """With an empty removable catalog the prompt should NOT carry a
    'Removable labels' header or a 'remove_labels' schema field — we
    don't want to spend tokens on a feature the user isn't using."""
    out = build_prompt("rules", catalog, [
        {"id": "x", "sender": "s", "subject": "j", "snippet": "k",
         "current_labels": ["Filed"]},
    ])
    assert "Removable labels" not in out
    assert "remove_labels" not in out
    # current_labels line is also suppressed when removable is off
    assert "Current labels:" not in out


def test_build_prompt_renders_removable_section_and_current_labels(
        removable_catalog):
    out = build_prompt("rules", removable_catalog, [
        {"id": "x", "sender": "s", "subject": "j", "snippet": "k",
         "current_labels": ["LegacyAuto", "Receipts"]},
    ])
    assert "Removable labels" in out
    assert "OldNewsletters" in out
    assert "LegacyAuto" in out
    assert "Current labels: LegacyAuto, Receipts" in out
    # Schema example mentions the new optional field
    assert '"remove_labels"' in out


def test_build_prompt_skips_current_labels_line_when_thread_has_none(
        removable_catalog):
    """A thread with no current_labels shouldn't emit a 'Current labels:'
    line even when the removable catalog is active."""
    out = build_prompt("rules", removable_catalog, [
        {"id": "x", "sender": "s", "subject": "j", "snippet": "k",
         "current_labels": []},
    ])
    assert "Removable labels" in out
    assert "Current labels:" not in out


def test_validate_decisions_strict_accepts_remove_labels(removable_catalog):
    """A keep decision with a valid `remove_labels` array carries it
    through to the validated output."""
    batch = [{"id": "a", "current_labels": ["LegacyAuto"]}]
    decs = [{"id": "a", "action": "keep", "label": "Receipts",
             "remove_labels": ["LegacyAuto"]}]
    good, missing, _ = validate_decisions_strict(decs, batch, removable_catalog)
    assert missing == set()
    assert good[0]["remove_labels"] == ["LegacyAuto"]


def test_validate_decisions_strict_drops_remove_labels_not_in_catalog(
        removable_catalog):
    """A label not in catalog.removable is dropped with an error event;
    the decision is still accepted."""
    batch = [{"id": "a", "current_labels": ["Receipts"]}]
    decs = [{"id": "a", "action": "keep", "label": "Receipts",
             "remove_labels": ["Receipts"]}]  # Receipts is keep, not removable
    good, missing, errs = validate_decisions_strict(decs, batch, removable_catalog)
    assert missing == set()
    assert "remove_labels" not in good[0]
    assert any("not in removable catalog" in e for e in errs)


def test_validate_decisions_strict_drops_remove_labels_not_on_thread(
        removable_catalog):
    """A label in the catalog but NOT on the thread is silently dropped
    (not an error — the model might just have stale label state)."""
    batch = [{"id": "a", "current_labels": ["Receipts"]}]
    decs = [{"id": "a", "action": "keep", "label": "Receipts",
             "remove_labels": ["LegacyAuto"]}]
    good, missing, errs = validate_decisions_strict(decs, batch, removable_catalog)
    assert missing == set()
    assert "remove_labels" not in good[0]
    # No error event for missing-from-thread — silent drop
    assert not any("not in removable catalog" in e for e in errs)


def test_validate_decisions_strict_strips_remove_labels_on_trash(
        removable_catalog):
    """Trash decisions silently drop remove_labels — trash hides labels
    anyway, so the strip would be wasted."""
    batch = [{"id": "a", "current_labels": ["LegacyAuto"]}]
    decs = [{"id": "a", "action": "trash", "label": None,
             "remove_labels": ["LegacyAuto"]}]
    good, missing, _ = validate_decisions_strict(decs, batch, removable_catalog)
    assert missing == set()
    assert "remove_labels" not in good[0]
    assert good[0]["action"] == "trash"


def test_validate_relabel_decisions_accepts_remove_labels(removable_catalog):
    batch = [{"id": "x", "current_labels": ["OldNewsletters"]}]
    out, missing, _ = validate_relabel_decisions(
        [{"id": "x", "label": "Receipts",
          "remove_labels": ["OldNewsletters"]}],
        batch, removable_catalog,
    )
    assert missing == set()
    assert out[0]["remove_labels"] == ["OldNewsletters"]


def test_build_relabel_prompt_renders_removable_section(removable_catalog):
    out = build_relabel_prompt(removable_catalog, [
        {"id": "x", "sender": "s", "subject": "j", "current_label": "Filed",
         "current_labels": ["Filed", "OldNewsletters"]},
    ])
    assert "Removable labels" in out
    assert "Current labels: Filed, OldNewsletters" in out
    assert '"remove_labels"' in out
