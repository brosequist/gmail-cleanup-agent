"""Tests for `_retry_gmail` — the wrapper that recovers from transient
SSL / socket errors during long-running classify passes.

This is the layer that survives a laptop-sleep mid-run, an SSL
handshake failure, or an HTTP 5xx blip without forcing the whole job
to fail and resume. Worth a focused test.
"""

from __future__ import annotations

import socket
import ssl
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from gmail_cleanup.gmail_client import _retry_gmail


def test_returns_immediately_on_success():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert _retry_gmail(fn) == "ok"
    assert len(calls) == 1


def test_retries_on_ssl_error_and_calls_rebuild(monkeypatch):
    """An SSLError on attempt 1 should trigger on_rebuild then retry."""
    monkeypatch.setattr("gmail_cleanup.gmail_client.time.sleep", lambda _s: None)

    calls = []
    rebuild_calls = []

    def fn():
        calls.append(1)
        if len(calls) < 2:
            raise ssl.SSLError("EOF occurred in violation of protocol")
        return "ok-after-retry"

    def rebuild():
        rebuild_calls.append(1)

    result = _retry_gmail(fn, on_rebuild=rebuild, attempts=3)
    assert result == "ok-after-retry"
    assert len(calls) == 2
    assert len(rebuild_calls) == 1


def test_retries_on_socket_error(monkeypatch):
    monkeypatch.setattr("gmail_cleanup.gmail_client.time.sleep", lambda _s: None)
    attempts = []

    def fn():
        attempts.append(1)
        if len(attempts) < 2:
            raise socket.error("Connection reset by peer")
        return 42

    assert _retry_gmail(fn, attempts=3) == 42


def test_giveup_after_max_attempts(monkeypatch):
    """When every attempt errors, a RuntimeError is raised with the
    last exception chained."""
    monkeypatch.setattr("gmail_cleanup.gmail_client.time.sleep", lambda _s: None)

    def fn():
        raise TimeoutError("read timeout")

    with pytest.raises(RuntimeError, match="failed after"):
        _retry_gmail(fn, attempts=3)


def test_5xx_http_error_retried(monkeypatch):
    monkeypatch.setattr("gmail_cleanup.gmail_client.time.sleep", lambda _s: None)
    attempts = []

    def fn():
        attempts.append(1)
        if len(attempts) < 2:
            # Build an HttpError with status 503
            resp = MagicMock()
            resp.status = 503
            raise HttpError(resp=resp, content=b"")
        return "ok"

    assert _retry_gmail(fn, attempts=3) == "ok"


def test_4xx_http_error_not_retried():
    """A 403 forbidden / 404 not found / 400 bad request must surface
    immediately — they won't fix themselves and retrying just delays
    the inevitable failure."""
    def fn():
        resp = MagicMock()
        resp.status = 404
        raise HttpError(resp=resp, content=b"")

    with pytest.raises(HttpError):
        _retry_gmail(fn, attempts=3)


def test_429_is_retried(monkeypatch):
    """429 IS retryable (rate limit, transient)."""
    monkeypatch.setattr("gmail_cleanup.gmail_client.time.sleep", lambda _s: None)
    attempts = []

    def fn():
        attempts.append(1)
        if len(attempts) < 2:
            resp = MagicMock()
            resp.status = 429
            raise HttpError(resp=resp, content=b"")
        return "ok"

    assert _retry_gmail(fn, attempts=3) == "ok"
