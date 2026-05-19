"""Tests for the real GmailClient.search_threads skip paths.

Gmail returns specific errors for messages it can't serve as
`format=metadata` — Google Chat history, drafts, and similar non-
standard messages. The classifier needs to skip those without aborting
the whole run.

We construct a GmailClient with a fully-faked service object so no
network calls happen. The chained `service.users().threads().list()`
+ `.messages().get()` shape is mimicked just enough to drive
search_threads through its skip-on-404 and skip-on-400-precondition
branches.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from gmail_cleanup.gmail_client import GmailClient


def _http_error(status: int, content: bytes = b"") -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=content)


class _ThreadsListExecutable:
    def __init__(self, page):
        self._page = page

    def execute(self):
        return self._page


class _MessagesGet:
    def __init__(self, *, raise_with: Exception | None = None, payload=None):
        self._raise = raise_with
        self._payload = payload or {
            "payload": {"headers": [
                {"name": "From", "value": "good@example.com"},
                {"name": "Subject", "value": "real subject"},
            ]},
            "internalDate": "1700000000000",
        }

    def execute(self):
        if self._raise is not None:
            raise self._raise
        return self._payload


class _Threads:
    """Implements .list() and .get() — the two endpoints search_threads
    walks through."""

    def __init__(self, page, msg_responses):
        self._page = page
        self._msg_responses = msg_responses  # dict of tid -> _MessagesGet

    def list(self, **_kwargs):
        return _ThreadsListExecutable(self._page)


class _Messages:
    def __init__(self, msg_responses):
        self._msg_responses = msg_responses

    def get(self, *, userId, id, format, metadataHeaders=None):  # noqa: A002
        return self._msg_responses[id]


class _Users:
    def __init__(self, threads, messages):
        self._threads = threads
        self._messages = messages

    def threads(self):
        return self._threads

    def messages(self):
        return self._messages


class _Service:
    def __init__(self, threads, messages):
        self._users = _Users(threads, messages)

    def users(self):
        return self._users


def _make_client(service) -> GmailClient:
    c = GmailClient(Path("/tmp/.creds-unused"), Path("/tmp/.token-unused"))
    c._service = service
    # avoid the real rebuild path (which would re-auth)
    c._rebuild_service = lambda: None
    return c


def test_search_threads_skips_404_messages():
    """A thread that 404s between threads.list and messages.get should
    not yield, and the iteration continues to the next thread."""
    page = {"threads": [
        {"id": "g1", "snippet": "vanished"},
        {"id": "g2", "snippet": "still here"},
    ]}
    msg_responses = {
        "g1": _MessagesGet(raise_with=_http_error(404)),
        "g2": _MessagesGet(),
    }
    svc = _Service(_Threads(page, msg_responses), _Messages(msg_responses))
    client = _make_client(svc)

    out = list(client.search_threads("anything"))
    assert [t.thread_id for t in out] == ["g2"]


def test_search_threads_skips_400_precondition_failed():
    """Gmail returns 400 'Precondition check failed' for Chat-history
    messages and drafts that can't be served as format=metadata. These
    should be skipped, not crash the iteration."""
    page = {"threads": [
        {"id": "chat1", "snippet": "chat history"},
        {"id": "real1", "snippet": "real email"},
    ]}
    msg_responses = {
        "chat1": _MessagesGet(
            raise_with=_http_error(400, b"Precondition check failed")),
        "real1": _MessagesGet(),
    }
    svc = _Service(_Threads(page, msg_responses), _Messages(msg_responses))
    client = _make_client(svc)

    out = list(client.search_threads("anything"))
    assert [t.thread_id for t in out] == ["real1"]


def test_search_threads_400_unknown_re_raises():
    """A 400 that's NOT a precondition-failed should bubble up — we
    don't want to swallow novel Gmail errors silently."""
    page = {"threads": [
        {"id": "x", "snippet": "?"},
    ]}
    msg_responses = {
        "x": _MessagesGet(raise_with=_http_error(400, b"Some other error")),
    }
    svc = _Service(_Threads(page, msg_responses), _Messages(msg_responses))
    client = _make_client(svc)

    with pytest.raises(HttpError):
        list(client.search_threads("anything"))


def test_search_threads_max_threads_caps_results():
    page = {"threads": [
        {"id": f"t{i}", "snippet": "s"} for i in range(5)
    ]}
    msg_responses = {f"t{i}": _MessagesGet() for i in range(5)}
    svc = _Service(_Threads(page, msg_responses), _Messages(msg_responses))
    client = _make_client(svc)

    out = list(client.search_threads("q", max_threads=3))
    assert len(out) == 3
    assert [t.thread_id for t in out] == ["t0", "t1", "t2"]
