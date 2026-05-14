"""Gmail API wrapper. OAuth flow + the few operations we need: search,
list-labels, create-label, apply-label, trash-thread."""

from __future__ import annotations

import json
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)


# Errors worth retrying on Gmail API calls. The googleapiclient library
# doesn't auto-recover when the underlying SSL connection dies (e.g. the
# laptop went to sleep mid-run), so we catch socket/SSL-level errors and
# rebuild the service before retrying.
_GMAIL_RETRYABLE = (
    ssl.SSLError,
    ssl.SSLEOFError,
    socket.error,
    ConnectionError,
    TimeoutError,
    OSError,
)


def _retry_gmail(fn, *, attempts: int = 6, on_rebuild=None):
    """Run `fn()` with retry on transient connection errors. `on_rebuild`
    is called between retries so the caller can recreate the underlying
    service object (the existing one's HTTP/SSL state is dead)."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except _GMAIL_RETRYABLE as e:
            last = e
            if i + 1 < attempts:
                delay = min(2 ** i, 30)
                logger.warning(
                    "gmail transient error (%s: %s) — retrying in %ds (attempt %d/%d)",
                    type(e).__name__, e, delay, i + 1, attempts,
                )
                if on_rebuild:
                    try:
                        on_rebuild()
                    except Exception as re:
                        logger.warning("rebuild failed: %s", re)
                time.sleep(delay)
                continue
            break
        except HttpError as e:
            # 5xx and 429 are retryable, 4xx other than 429 are not.
            status = getattr(e.resp, "status", 0)
            if status in (429, 500, 502, 503, 504) and i + 1 < attempts:
                delay = min(2 ** i, 30)
                logger.warning(
                    "gmail HTTP %d — retrying in %ds (attempt %d/%d)",
                    status, delay, i + 1, attempts,
                )
                time.sleep(delay)
                continue
            raise
    raise RuntimeError(
        f"Gmail API call failed after {attempts} attempts: "
        f"{type(last).__name__}: {last}"
    ) from last

# `gmail.modify` lets us read, label, and trash. It does NOT permit
# permanent-delete or sending — that's intentional. The narrow scope is the
# main user-trust argument: the worst the tool can do is trash mail
# (recoverable for 30 days), apply labels, or read.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


@dataclass
class ThreadSummary:
    """Compact view of a thread used for classification — never includes
    full body unless explicitly fetched separately."""
    thread_id: str
    sender: str
    subject: str
    snippet: str
    date: str


class GmailClient:
    def __init__(self, credentials_path: Path, token_path: Path):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._service = None

    # ---------- auth ----------

    def authorize(self, force: bool = False) -> None:
        """Run the OAuth flow if needed. Persists token.json with refresh
        token so subsequent runs don't re-prompt."""
        creds = self._load_creds()
        if force:
            creds = None
        if creds and creds.valid:
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            return
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not self.credentials_path.exists():
                raise FileNotFoundError(
                    f"Missing OAuth client config at {self.credentials_path}. "
                    "Follow docs/oauth-setup.md."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.credentials_path), SCOPES
            )
            # local server flow opens a browser tab. port=0 = pick a free port.
            creds = flow.run_local_server(port=0)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _load_creds(self) -> Credentials | None:
        if not self.token_path.exists():
            return None
        try:
            return Credentials.from_authorized_user_info(
                json.loads(self.token_path.read_text()), SCOPES
            )
        except Exception:
            return None

    @property
    def service(self):
        if self._service is None:
            self.authorize()
        return self._service

    def _rebuild_service(self):
        """Force a fresh Gmail service. Used when the underlying SSL
        connection has been broken (e.g. laptop sleep). Re-uses cached
        OAuth credentials — does NOT re-prompt the user."""
        creds = self._load_creds()
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self.token_path.write_text(creds.to_json())
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    # ---------- labels ----------

    def list_labels(self) -> dict[str, str]:
        """Return {label_name: label_id} for all labels in the account
        (system labels included)."""
        def _do():
            return self.service.users().labels().list(userId="me").execute()
        res = _retry_gmail(_do, on_rebuild=self._rebuild_service)
        return {l["name"]: l["id"] for l in res.get("labels", [])}

    def create_label(self, name: str) -> str:
        """Create a user label, return its ID. Idempotent."""
        existing = self.list_labels()
        if name in existing:
            return existing[name]
        body = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        def _do():
            return self.service.users().labels().create(userId="me", body=body).execute()
        res = _retry_gmail(_do, on_rebuild=self._rebuild_service)
        return res["id"]

    # ---------- threads ----------

    def search_threads(
        self, query: str, page_size: int = 100, max_threads: int | None = None,
        skip_ids: set[str] | None = None,
    ) -> Iterator[ThreadSummary]:
        """Yield ThreadSummary objects matching the query. Handles
        pagination transparently. Reads only the first message of each
        thread (which is what's useful for triage). Stops at max_threads
        if set.

        If `skip_ids` is provided, threads whose ID is in the set are
        yielded as bare placeholders (empty sender/subject/date) so the
        caller can resume-skip them without paying for a per-thread
        `messages.get` round-trip. Critical for runs with large
        state.json on resume.
        """
        page_token: str | None = None
        yielded = 0
        while True:
            def _list_page(pt=page_token):
                return self.service.users().threads().list(
                    userId="me",
                    q=query,
                    maxResults=page_size,
                    pageToken=pt,
                ).execute()
            res = _retry_gmail(_list_page, on_rebuild=self._rebuild_service)

            for t in res.get("threads", []):
                tid = t["id"]
                snippet = t.get("snippet", "")
                if skip_ids is not None and tid in skip_ids:
                    yield ThreadSummary(
                        thread_id=tid, sender="", subject="", snippet="", date="",
                    )
                    yielded += 1
                    if max_threads and yielded >= max_threads:
                        return
                    continue
                # Fetch first message metadata (cheaper than full thread)
                def _get_meta(tid=tid):
                    return self.service.users().messages().get(
                        userId="me",
                        id=tid,
                        format="metadata",
                        metadataHeaders=["From", "Subject", "Date"],
                    ).execute()
                try:
                    meta = _retry_gmail(_get_meta, on_rebuild=self._rebuild_service)
                except HttpError as e:
                    # 404 happens when a message is deleted/spammed between
                    # the threads.list call and this messages.get — race
                    # condition with normal mailbox activity (Google's spam
                    # filter, manual cleanup elsewhere). Skip and continue
                    # rather than abort the entire run.
                    if getattr(e.resp, "status", 0) == 404:
                        logger.warning("thread %s vanished between list and fetch; skipping", tid)
                        continue
                    raise
                hdrs = {h["name"]: h["value"] for h in meta.get("payload", {}).get("headers", [])}
                yield ThreadSummary(
                    thread_id=tid,
                    sender=hdrs.get("From", ""),
                    subject=hdrs.get("Subject", "(no subject)"),
                    snippet=snippet,
                    date=hdrs.get("Date", ""),
                )
                yielded += 1
                if max_threads and yielded >= max_threads:
                    return

            page_token = res.get("nextPageToken")
            if not page_token:
                break

    def trash_thread(self, thread_id: str) -> None:
        def _do():
            return self.service.users().threads().trash(
                userId="me", id=thread_id
            ).execute()
        _retry_gmail(_do, on_rebuild=self._rebuild_service)

    def add_label_to_thread(self, thread_id: str, label_id: str) -> None:
        body = {"addLabelIds": [label_id], "removeLabelIds": []}
        def _do():
            return self.service.users().threads().modify(
                userId="me", id=thread_id, body=body
            ).execute()
        _retry_gmail(_do, on_rebuild=self._rebuild_service)
