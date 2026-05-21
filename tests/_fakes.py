"""Reusable fake objects shared across test files.

Lives outside conftest.py so test modules can `from tests._fakes import
FakeBackend, FakeGmailClient` directly. (conftest.py defines fixtures,
which are auto-discovered but not directly importable in a clean way.)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gmail_cleanup.gmail_client import ThreadSummary


# ---------- Gmail-side fakes (apply-log path) ----------


@dataclass
class FakeBatch:
    """Stand-in for `service.new_batch_http_request(callback=...)`."""
    callback: callable
    requests: list = field(default_factory=list)

    def add(self, request, request_id):
        self.requests.append((request_id, request))

    def execute(self):
        for rid, req in self.requests:
            self.callback(rid, req.response, req.exception)


@dataclass
class FakeRequest:
    action: str
    tid: str
    response: object = None
    exception: Exception | None = None


class FakeService:
    """Mimics enough of googleapiclient's chained shape to satisfy
    GmailClient and the apply-log code path."""

    def __init__(self):
        self._failures: dict[str, Exception] = {}
        self._modify_calls: list[tuple[str, dict]] = []
        self._trash_calls: list[str] = []
        self._label_create_calls: list[str] = []
        self.labels = {"Receipts": "Label_1", "Family": "Label_2"}

    def set_failure(self, tid: str, exc: Exception):
        self._failures[tid] = exc

    def users(self):
        return self

    def threads(self):
        return self

    def messages(self):
        return self

    def trash(self, *, userId, id):  # noqa: A002
        self._trash_calls.append(id)
        return FakeRequest(action="trash", tid=id, exception=self._failures.get(id))

    def modify(self, *, userId, id, body):  # noqa: A002
        self._modify_calls.append((id, body))
        return FakeRequest(action="modify", tid=id, exception=self._failures.get(id))

    def list(self, *, userId, **kwargs):  # noqa: A002
        return _Executable({"labels": [{"name": n, "id": i}
                                       for n, i in self.labels.items()]})

    def create(self, *, userId, body):  # noqa: A002
        name = body["name"]
        self._label_create_calls.append(name)
        new_id = f"Label_{len(self.labels) + 1}"
        self.labels[name] = new_id
        return _Executable({"id": new_id, "name": name})

    def new_batch_http_request(self, callback):
        return FakeBatch(callback=callback)


class _Executable:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class FakeGmailClient:
    """Drop-in replacement for GmailClient in CLI integration tests."""

    def __init__(self, threads_to_yield: list[ThreadSummary] | None = None,
                 labels: dict[str, str] | None = None):
        self.threads_to_yield = threads_to_yield or []
        self._labels = dict(labels) if labels is not None else {
            "Receipts": "Label_1", "Family": "Label_2"}
        self.trashed: list[str] = []
        self.labeled: list[tuple[str, str]] = []
        self.modified: list[dict] = []
        self.created_labels: list[str] = []
        self._authorized = False
        self._service = FakeService()
        self._service.labels = self._labels
        self.last_include_body: bool | None = None
        self.last_query: str | None = None
        # Thread IDs whose mutation calls should raise — lets tests
        # exercise the apply-failure -> error path.
        self.fail_on: set[str] = set()

    def authorize(self, force: bool = False) -> None:
        self._authorized = True

    def list_labels(self) -> dict[str, str]:
        return dict(self._labels)

    def create_label(self, name: str) -> str:
        new_id = f"Label_{len(self._labels) + 1}"
        self._labels[name] = new_id
        self.created_labels.append(name)
        return new_id

    def search_threads(self, query, *, max_threads=None, skip_ids=None,
                       include_body=False, page_size=100):
        self.last_query = query
        self.last_include_body = include_body
        skip_ids = skip_ids or set()
        n = 0
        for t in self.threads_to_yield:
            if max_threads is not None and n >= max_threads:
                return
            if t.thread_id in skip_ids:
                yield ThreadSummary(thread_id=t.thread_id, sender="",
                                    subject="", snippet="", date="")
                n += 1
                continue
            yield t
            n += 1

    def trash_thread(self, tid: str) -> None:
        if tid in self.fail_on:
            raise RuntimeError(f"simulated Gmail failure for {tid}")
        self.trashed.append(tid)

    def add_label_to_thread(self, tid: str, label_id: str) -> None:
        if tid in self.fail_on:
            raise RuntimeError(f"simulated Gmail failure for {tid}")
        self.labeled.append((tid, label_id))

    def modify_thread_labels(self, tid: str,
                             add_label_ids=None, remove_label_ids=None) -> None:
        if tid in self.fail_on:
            raise RuntimeError(f"simulated Gmail failure for {tid}")
        self.modified.append({"id": tid, "add": add_label_ids or [],
                              "remove": remove_label_ids or []})

    def fetch_thread_meta(self, tid: str):
        for t in self.threads_to_yield:
            if t.thread_id == tid:
                return t
        return None


# ---------- LLM-side fake ----------


class FakeBackend:
    """Stand-in for an LLM backend. `responses` is a queue; each
    classify_batch() pops the next. When down to one entry it sticks."""

    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or [])
        self.prompts: list[str] = []

    def classify_batch(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            return '{"decisions": []}'
        if len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)
