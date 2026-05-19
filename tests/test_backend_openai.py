"""Tests for the OpenAI-compatible backend's env-driven configuration.

These don't make real OpenAI API calls — they stub the SDK client to
inspect what request kwargs the backend builds. Skipped automatically
if the `openai` extra isn't installed.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Skip the whole module if the openai SDK isn't available — the project
# treats it as an optional extra.
pytest.importorskip("openai")


def _fresh_backend(monkeypatch, env: dict) -> "object":
    """Re-import the backend module with a clean env so module-level
    state (none today, but defensive) doesn't leak between tests."""
    for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
              "OPENAI_MAX_TOKENS", "OPENAI_TEMPERATURE", "OPENAI_JSON_MODE",
              "OPENAI_DISABLE_THINKING", "OPENAI_RETRIES"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from gmail_cleanup.backends import openai as oa
    importlib.reload(oa)
    return oa


def test_defaults_when_env_empty(monkeypatch):
    oa = _fresh_backend(monkeypatch, {})
    b = oa.OpenAIBackend()
    assert b.model == "gpt-4o-mini"
    assert b.json_mode is True
    assert b.retries == 5


def test_json_mode_disable(monkeypatch):
    oa = _fresh_backend(monkeypatch, {"OPENAI_JSON_MODE": "0"})
    assert oa.OpenAIBackend().json_mode is False


def test_disable_thinking_adds_extra_body(monkeypatch):
    """OPENAI_DISABLE_THINKING=1 should send chat_template_kwargs in
    extra_body so reasoning models route straight to `content`."""
    oa = _fresh_backend(monkeypatch, {"OPENAI_DISABLE_THINKING": "1"})
    b = oa.OpenAIBackend()

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        # Return a SimpleNamespace mimicking openai's response shape
        msg = SimpleNamespace(content='{"decisions":[]}')
        choice = SimpleNamespace(message=msg)
        return SimpleNamespace(choices=[choice])

    b.client = MagicMock()
    b.client.chat.completions.create = fake_create

    b.classify_batch("prompt")
    assert captured.get("extra_body") == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_thinking_default_does_not_add_extra_body(monkeypatch):
    oa = _fresh_backend(monkeypatch, {})
    b = oa.OpenAIBackend()
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        msg = SimpleNamespace(content='{"decisions":[]}')
        choice = SimpleNamespace(message=msg)
        return SimpleNamespace(choices=[choice])

    b.client = MagicMock()
    b.client.chat.completions.create = fake_create

    b.classify_batch("prompt")
    assert "extra_body" not in captured


def test_json_mode_off_omits_response_format(monkeypatch):
    oa = _fresh_backend(monkeypatch, {"OPENAI_JSON_MODE": "0"})
    b = oa.OpenAIBackend()
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        msg = SimpleNamespace(content='{"decisions":[]}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    b.client = MagicMock()
    b.client.chat.completions.create = fake_create
    b.classify_batch("prompt")
    assert "response_format" not in captured


def test_retries_on_transient_then_succeeds(monkeypatch):
    """A retryable error (RateLimitError) on the first call should be
    retried; the next success returns normally."""
    monkeypatch.setattr("gmail_cleanup.backends.openai.time.sleep",
                        lambda _s: None)
    oa = _fresh_backend(monkeypatch, {"OPENAI_RETRIES": "3"})
    b = oa.OpenAIBackend()

    from openai import RateLimitError
    attempts = []

    def fake_create(**_kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            # Construct a minimal RateLimitError stand-in. The SDK's
            # init signature varies across versions; raising the bare
            # class via __new__ avoids the constructor entirely.
            raise RateLimitError.__new__(RateLimitError)
        msg = SimpleNamespace(content='{"decisions":[]}')
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    b.client = MagicMock()
    b.client.chat.completions.create = fake_create
    out = b.classify_batch("p")
    assert out == '{"decisions":[]}'
    assert len(attempts) == 2


def test_no_retry_on_non_retryable(monkeypatch):
    """A semantic 4xx (auth / bad request) MUST bubble up without
    burning retries — those errors won't fix themselves."""
    oa = _fresh_backend(monkeypatch, {"OPENAI_RETRIES": "5"})
    b = oa.OpenAIBackend()

    class _BadRequest(Exception):
        pass

    attempts = []

    def fake_create(**_kwargs):
        attempts.append(1)
        raise _BadRequest("invalid model id")

    b.client = MagicMock()
    b.client.chat.completions.create = fake_create
    with pytest.raises(_BadRequest):
        b.classify_batch("p")
    assert len(attempts) == 1  # NOT retried
