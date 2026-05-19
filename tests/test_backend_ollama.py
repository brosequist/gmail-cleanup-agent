"""Tests for OllamaBackend — exercises the httpx client by replacing
its `_client` attribute with a fake. Covers the happy path plus the
retry-on-transient-error path that keeps long-running classify passes
alive across `kubectl port-forward` blips.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import httpx
import pytest


def _fresh(monkeypatch, env: dict | None = None):
    for k in ("OLLAMA_HOST", "OLLAMA_MODEL", "OLLAMA_NUM_CTX",
              "OLLAMA_KEEP_ALIVE", "OLLAMA_TIMEOUT", "OLLAMA_RETRIES"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    from gmail_cleanup.backends import ollama as o
    importlib.reload(o)
    return o


def test_defaults_when_env_empty(monkeypatch):
    o = _fresh(monkeypatch)
    b = o.OllamaBackend()
    assert b.host == "http://localhost:11434"
    assert b.model == "qwen3.6:35b-a3b-iq3_xxs-fixed"
    assert b.num_ctx == 8192
    assert b.keep_alive == "60m"
    assert b.retries == 5


def test_env_overrides(monkeypatch):
    o = _fresh(monkeypatch, {
        "OLLAMA_HOST": "http://gpu-node:11434/",  # trailing slash stripped
        "OLLAMA_MODEL": "qwen3:8b",
        "OLLAMA_NUM_CTX": "4096",
        "OLLAMA_KEEP_ALIVE": "5m",
        "OLLAMA_TIMEOUT": "120",
        "OLLAMA_RETRIES": "2",
    })
    b = o.OllamaBackend()
    assert b.host == "http://gpu-node:11434"  # trailing / stripped
    assert b.model == "qwen3:8b"
    assert b.num_ctx == 4096
    assert b.keep_alive == "5m"
    assert b.timeout == 120.0
    assert b.retries == 2


def test_classify_batch_happy_path(monkeypatch):
    o = _fresh(monkeypatch)
    b = o.OllamaBackend()

    captured = {}

    def post(url, json):
        captured["url"] = url
        captured["body"] = json
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={
            "message": {"content": '{"decisions":[]}'}})
        return resp

    b._client.post = post
    out = b.classify_batch("test prompt")
    assert out == '{"decisions":[]}'
    assert captured["url"].endswith("/api/chat")
    assert captured["body"]["model"] == b.model
    assert captured["body"]["format"] == "json"
    assert captured["body"]["messages"][0]["content"] == "test prompt"


def test_classify_batch_retries_on_connection_error(monkeypatch):
    """A transient ConnectError on the first call (e.g. kubectl
    port-forward blip) should be retried; the second attempt succeeds."""
    monkeypatch.setattr("gmail_cleanup.backends.ollama.time.sleep",
                        lambda _s: None)
    o = _fresh(monkeypatch, {"OLLAMA_RETRIES": "3"})
    b = o.OllamaBackend()

    attempts = []

    def post(url, json):
        attempts.append(1)
        if len(attempts) < 2:
            raise httpx.ConnectError("port-forward died")
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"message": {"content": "{}"}})
        return resp

    b._client.post = post
    assert b.classify_batch("p") == "{}"
    assert len(attempts) == 2


def test_classify_batch_giveup_after_retries(monkeypatch):
    """After OLLAMA_RETRIES exhausted attempts, raises RuntimeError
    (chained from the last httpx exception)."""
    monkeypatch.setattr("gmail_cleanup.backends.ollama.time.sleep",
                        lambda _s: None)
    o = _fresh(monkeypatch, {"OLLAMA_RETRIES": "2"})
    b = o.OllamaBackend()

    def post(url, json):
        raise httpx.ConnectError("ollama down")

    b._client.post = post
    with pytest.raises(RuntimeError, match="ollama call failed"):
        b.classify_batch("p")


def test_close_is_idempotent(monkeypatch):
    o = _fresh(monkeypatch)
    b = o.OllamaBackend()
    b.close()
    b.close()  # must not raise on a second call
