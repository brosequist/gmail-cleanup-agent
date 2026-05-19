"""Tests for the Claude backend env handling. Parallel to test_backend_openai.

Skipped if `anthropic` SDK isn't installed (Claude is an optional extra).
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("anthropic")


def _fresh(monkeypatch, env: dict):
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_MODEL", "CLAUDE_MAX_TOKENS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from gmail_cleanup.backends import claude as c
    importlib.reload(c)
    return c


def test_missing_api_key_raises(monkeypatch):
    c = _fresh(monkeypatch, {})  # no ANTHROPIC_API_KEY
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        c.ClaudeBackend()


def test_defaults_when_only_key_set(monkeypatch):
    c = _fresh(monkeypatch, {"ANTHROPIC_API_KEY": "sk-ant-test"})
    b = c.ClaudeBackend()
    assert b.model == "claude-haiku-4-5-20251001"
    assert b.max_tokens == 4000


def test_model_and_max_tokens_overrides(monkeypatch):
    c = _fresh(monkeypatch, {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "CLAUDE_MODEL": "claude-opus-4-7",
        "CLAUDE_MAX_TOKENS": "8000",
    })
    b = c.ClaudeBackend()
    assert b.model == "claude-opus-4-7"
    assert b.max_tokens == 8000


def test_classify_batch_calls_messages_create(monkeypatch):
    c = _fresh(monkeypatch, {"ANTHROPIC_API_KEY": "sk-ant-test"})
    b = c.ClaudeBackend()

    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        # Mimic anthropic's response shape: msg.content is a list of
        # blocks, each with a .text attribute on text blocks.
        block = SimpleNamespace(text='{"decisions":[]}')
        return SimpleNamespace(content=[block])

    b.client = MagicMock()
    b.client.messages.create = fake_create

    out = b.classify_batch("hello prompt")
    assert out == '{"decisions":[]}'
    assert captured["model"] == "claude-haiku-4-5-20251001"
    assert captured["max_tokens"] == 4000
    assert captured["messages"][0]["content"] == "hello prompt"


def test_classify_batch_concatenates_multiple_text_blocks(monkeypatch):
    """Claude can split responses across multiple text blocks; the backend
    should concatenate them in order."""
    c = _fresh(monkeypatch, {"ANTHROPIC_API_KEY": "sk-ant-test"})
    b = c.ClaudeBackend()

    def fake_create(**kwargs):
        blocks = [
            SimpleNamespace(text='{"deci'),
            SimpleNamespace(text='sions":['),
            SimpleNamespace(text="]}"),
            # A non-text tool_use block should be skipped without crashing
            SimpleNamespace(),
        ]
        return SimpleNamespace(content=blocks)

    b.client = MagicMock()
    b.client.messages.create = fake_create
    assert b.classify_batch("p") == '{"decisions":[]}'
