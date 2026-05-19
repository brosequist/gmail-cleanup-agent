"""Unit tests for the pure helpers in gmail_client.py.

These don't touch the Gmail API — they cover:
  - _compute_age_days   (string/int/None/invalid)
  - _decode_b64url      (Gmail's URL-safe base64 with stripped padding)
  - _extract_body_text  (the MIME walker used by --include-body)
"""

from __future__ import annotations

import base64
import time

from gmail_cleanup.gmail_client import (
    BODY_MAX_CHARS,
    _compute_age_days,
    _decode_b64url,
    _extract_body_text,
)


# ---------------- _compute_age_days ----------------


def test_compute_age_days_none():
    assert _compute_age_days(None) is None


def test_compute_age_days_invalid():
    assert _compute_age_days("not-a-number") is None


def test_compute_age_days_future_clamped_to_zero():
    future_ms = int((time.time() + 3600) * 1000)
    assert _compute_age_days(future_ms) == 0


def test_compute_age_days_string_and_int_equivalent():
    ten_days_ago_ms = int((time.time() - 10 * 86400) * 1000)
    assert _compute_age_days(ten_days_ago_ms) == 10
    assert _compute_age_days(str(ten_days_ago_ms)) == 10


# ---------------- _decode_b64url ----------------


def test_decode_b64url_roundtrip():
    payload = b"hello world\nLine 2"
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    assert _decode_b64url(encoded) == "hello world\nLine 2"


def test_decode_b64url_handles_padding():
    # Length 5 → needs 3 chars of padding when decoded
    payload = b"hi"
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    assert _decode_b64url(encoded) == "hi"


def test_decode_b64url_unicode_replacement_on_bad_bytes():
    # Garbage that's still valid base64 but produces non-UTF8 bytes
    # — we want graceful degradation, not an exception.
    bad = base64.urlsafe_b64encode(b"\xff\xfe\xfa").decode().rstrip("=")
    out = _decode_b64url(bad)
    assert isinstance(out, str)
    # Replacement chars are fine; the test is that we returned.


# ---------------- _extract_body_text ----------------


def _b64(s: bytes) -> str:
    return base64.urlsafe_b64encode(s).decode().rstrip("=")


def test_extract_body_text_single_part_text_plain():
    payload = {
        "mimeType": "text/plain",
        "body": {"data": _b64(b"Hello there")},
    }
    assert _extract_body_text(payload) == "Hello there"


def test_extract_body_text_multipart_prefers_plain_over_html():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64(b"PLAIN")}},
            {"mimeType": "text/html",
             "body": {"data": _b64(b"<p>HTML</p>")}},
        ],
    }
    assert _extract_body_text(payload) == "PLAIN"


def test_extract_body_text_html_fallback_strips_tags():
    payload = {
        "mimeType": "text/html",
        "parts": [
            {"mimeType": "text/html",
             "body": {"data": _b64(
                 b"<style>p{color:red}</style><p>Visible <b>text</b></p>")}},
        ],
    }
    out = _extract_body_text(payload)
    assert "Visible" in out
    assert "text" in out
    assert "<p>" not in out
    assert "color:red" not in out  # style block removed


def test_extract_body_text_nested_multipart():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain",
                     "body": {"data": _b64(b"deep plain")}},
                ],
            },
            {"mimeType": "image/png", "body": {}},
        ],
    }
    assert _extract_body_text(payload) == "deep plain"


def test_extract_body_text_empty_payload():
    assert _extract_body_text({}) == ""
    assert _extract_body_text({"mimeType": "image/jpeg"}) == ""


def test_body_max_chars_is_sane():
    # Sanity: 4 KB cap protects token costs while preserving the meaningful
    # first paragraph of nearly every promotional email.
    assert 1_000 <= BODY_MAX_CHARS <= 16_000


def test_extract_body_text_returns_full_text_caller_truncates():
    """The extractor itself does NOT truncate — the cap is applied at
    the call site (search_threads) after extraction. Verify the
    extractor returns the full text so the caller's slice is what
    actually enforces BODY_MAX_CHARS."""
    huge = b"x" * (BODY_MAX_CHARS * 3)  # 3x the cap
    payload = {"mimeType": "text/plain", "body": {"data": _b64(huge)}}
    out = _extract_body_text(payload)
    assert len(out) == len(huge)
    # And the caller's slice [:BODY_MAX_CHARS] would shrink it to the cap
    assert len(out[:BODY_MAX_CHARS]) == BODY_MAX_CHARS
