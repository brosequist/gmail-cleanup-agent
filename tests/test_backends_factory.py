"""Tests for the GCA_BACKEND env-var switch in backends/__init__.py.

Backend factory bugs are silent at import time and only surface when a
user picks an unfamiliar GCA_BACKEND value — exactly the kind of bug
worth a fast unit test.
"""

from __future__ import annotations

import pytest

from gmail_cleanup.backends import get_backend
from gmail_cleanup.backends.ollama import OllamaBackend


def test_default_is_ollama(monkeypatch):
    monkeypatch.delenv("GCA_BACKEND", raising=False)
    b = get_backend()
    assert isinstance(b, OllamaBackend)


def test_ollama_explicit(monkeypatch):
    monkeypatch.setenv("GCA_BACKEND", "ollama")
    assert isinstance(get_backend(), OllamaBackend)


def test_openai_backend(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.setenv("GCA_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder")
    from gmail_cleanup.backends.openai import OpenAIBackend
    assert isinstance(get_backend(), OpenAIBackend)


def test_claude_backend(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.setenv("GCA_BACKEND", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    from gmail_cleanup.backends.claude import ClaudeBackend
    assert isinstance(get_backend(), ClaudeBackend)


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("GCA_BACKEND", "made-up-backend")
    with pytest.raises(ValueError, match="Unknown GCA_BACKEND"):
        get_backend()


def test_backend_name_case_insensitive(monkeypatch):
    monkeypatch.setenv("GCA_BACKEND", "OLLAMA")  # upper-case
    assert isinstance(get_backend(), OllamaBackend)
