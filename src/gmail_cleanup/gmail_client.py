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


def _compute_age_days(internal_date_ms: str | int | None) -> int | None:
    """Convert Gmail's `internalDate` (ms since epoch, as a string) into
    a count of days from now. Returns None when the value is missing or
    unparseable so the prompt builder can omit the field."""
    if internal_date_ms is None:
        return None
    try:
        ms = int(internal_date_ms)
    except (TypeError, ValueError):
        return None
    age_seconds = time.time() - ms / 1000.0
    if age_seconds < 0:
        return 0
    return int(age_seconds // 86400)


@dataclass
class ThreadSummary:
    """Compact view of a thread used for classification.

    `body` is empty by default — the classifier reads only snippet + a
    couple of header fields. Set `include_body=True` on `search_threads`
    (and pass `--include-body` from the CLI) to populate it from the
    first message's text part, capped at `BODY_MAX_CHARS` to keep
    prompts cheap.
    """
    thread_id: str
    sender: str
    subject: str
    snippet: str
    date: str
    # Optional metadata signals (None for older saved summaries that
    # predate these fields):
    age_days: int | None = None
    has_list_unsubscribe: bool = False
    body: str = ""


# Hard cap on per-email body text in the prompt. The first 4 KB of a
# message is almost always enough to disambiguate a borderline case
# (the snippet is ~200 chars of the same content); paying for the full
# body of every email in a 312k-thread run would blow up token costs.
BODY_MAX_CHARS = 4000


def _decode_b64url(data: str) -> str:
    """Gmail returns body parts as URL-safe base64 (no padding). Decode
    leniently — bad bytes become U+FFFD rather than raising."""
    import base64
    # Restore padding (b64 requires len % 4 == 0).
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding).decode(
            "utf-8", errors="replace"
        )
    except Exception:  # noqa: BLE001
        return ""


def _extract_body_text(payload: dict) -> str:
    """Walk a Gmail message payload tree and return the first text/plain
    body found (falling back to text/html → stripped of tags). Returns
    "" if no usable text part exists (rare — common for image-only or
    purely structural messages).
    """
    if not payload:
        return ""

    # Single-part messages have body.data directly on the root payload.
    mime = payload.get("mimeType", "")
    data = (payload.get("body") or {}).get("data")
    if data and mime.startswith("text/plain"):
        return _decode_b64url(data)

    # Multipart — DFS, prefer text/plain over text/html.
    html_fallback = ""
    stack = list(payload.get("parts", []) or [])
    while stack:
        part = stack.pop(0)
        sub_mime = part.get("mimeType", "")
        sub_data = (part.get("body") or {}).get("data")
        if sub_data:
            if sub_mime.startswith("text/plain"):
                return _decode_b64url(sub_data)
            if sub_mime.startswith("text/html") and not html_fallback:
                html_fallback = _decode_b64url(sub_data)
        if part.get("parts"):
            stack = list(part["parts"]) + stack

    if html_fallback:
        # Crude HTML strip — sufficient for "is this marketing" judgement,
        # not a full sanitizer. We deliberately don't depend on BS4.
        import re as _re
        text = _re.sub(r"<script[^>]*>.*?</script>", " ",
                       html_fallback, flags=_re.DOTALL | _re.IGNORECASE)
        text = _re.sub(r"<style[^>]*>.*?</style>", " ", text,
                       flags=_re.DOTALL | _re.IGNORECASE)
        text = _re.sub(r"<[^>]+>", " ", text)
        text = _re.sub(r"\s+", " ", text).strip()
        return text

    # Root payload itself had data but mime wasn't text/plain — try it
    # anyway (some non-standard senders).
    if data:
        return _decode_b64url(data)
    return ""


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
        skip_ids: set[str] | None = None, include_body: bool = False,
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

        If `include_body=True`, each fetched thread also pulls the first
        message's body (text/plain preferred, text/html fallback) and
        truncates it to BODY_MAX_CHARS. Doubles or triples the per-
        message API payload — use sparingly.
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
                # Fetch first message metadata (cheaper than full thread).
                # `format=full` is required to get body parts; metadata-only
                # is enough when --include-body is off.
                fmt = "full" if include_body else "metadata"
                def _get_meta(tid=tid, fmt=fmt):
                    kwargs = {"userId": "me", "id": tid, "format": fmt}
                    if fmt == "metadata":
                        kwargs["metadataHeaders"] = [
                            "From", "Subject", "Date", "List-Unsubscribe",
                        ]
                    return self.service.users().messages().get(**kwargs).execute()
                try:
                    meta = _retry_gmail(_get_meta, on_rebuild=self._rebuild_service)
                except HttpError as e:
                    status = getattr(e.resp, "status", 0)
                    # 404: message vanished between threads.list and this
                    # messages.get — race with normal mailbox activity
                    # (Google's spam filter, manual cleanup elsewhere).
                    if status == 404:
                        logger.warning("thread %s vanished between list and fetch; skipping", tid)
                        continue
                    # 400 "Precondition check failed": Gmail can't return
                    # format=metadata for this message — typically a Google
                    # Chat history message synced into Gmail, a draft, or
                    # another non-standard kind that lacks From/Subject/Date.
                    # One bad apple per ~tens-of-thousands of messages; skip
                    # rather than abort the whole run.
                    if status == 400 and b"recondition" in (e.content or b""):
                        logger.warning(
                            "thread %s rejected by Gmail (precondition failed; "
                            "likely a Chat/draft message without headers); skipping",
                            tid,
                        )
                        continue
                    raise
                hdrs = {h["name"]: h["value"] for h in meta.get("payload", {}).get("headers", [])}
                body = ""
                if include_body:
                    body = _extract_body_text(meta.get("payload", {}))[:BODY_MAX_CHARS]
                yield ThreadSummary(
                    thread_id=tid,
                    sender=hdrs.get("From", ""),
                    subject=hdrs.get("Subject", "(no subject)"),
                    snippet=snippet,
                    date=hdrs.get("Date", ""),
                    age_days=_compute_age_days(meta.get("internalDate")),
                    has_list_unsubscribe=bool(hdrs.get("List-Unsubscribe")),
                    body=body,
                )
                yielded += 1
                if max_threads and yielded >= max_threads:
                    return

            page_token = res.get("nextPageToken")
            if not page_token:
                break

    def fetch_thread_meta(self, thread_id: str) -> ThreadSummary | None:
        """Fetch From/Subject/Date + snippet for a single thread by ID.
        Returns None if the thread no longer exists (404 — deleted or
        spammed since it was first seen). Used by the relabel pass to
        re-hydrate snippets that the decision log doesn't store."""
        def _get(tid=thread_id):
            return self.service.users().threads().get(
                userId="me", id=tid, format="metadata",
                metadataHeaders=["From", "Subject", "Date",
                                 "List-Unsubscribe"],
            ).execute()
        try:
            th = _retry_gmail(_get, on_rebuild=self._rebuild_service)
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            if status == 404:
                return None
            # See search_threads for context: Gmail returns 400
            # "Precondition check failed" for messages it can't serve as
            # format=metadata (Chat history, drafts, etc.). Treat as
            # "no usable metadata" rather than aborting the caller.
            if status == 400 and b"recondition" in (e.content or b""):
                logger.warning(
                    "thread %s rejected by Gmail (precondition failed); "
                    "returning None",
                    thread_id,
                )
                return None
            raise
        msgs = th.get("messages", [])
        if not msgs:
            return None
        first = msgs[0]
        hdrs = {h["name"]: h["value"] for h in first.get("payload", {}).get("headers", [])}
        return ThreadSummary(
            thread_id=thread_id,
            sender=hdrs.get("From", ""),
            subject=hdrs.get("Subject", "(no subject)"),
            snippet=first.get("snippet", ""),
            date=hdrs.get("Date", ""),
            age_days=_compute_age_days(first.get("internalDate")),
            has_list_unsubscribe=bool(hdrs.get("List-Unsubscribe")),
        )

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

    def modify_thread_labels(
        self, thread_id: str,
        add_label_ids: list[str] | None = None,
        remove_label_ids: list[str] | None = None,
    ) -> None:
        """Add and/or remove labels on a thread in a single modify call.
        Gmail's modify is idempotent — removing a label the thread does
        not have is a harmless no-op, so callers don't need to check
        membership first."""
        body = {
            "addLabelIds": add_label_ids or [],
            "removeLabelIds": remove_label_ids or [],
        }
        def _do():
            return self.service.users().threads().modify(
                userId="me", id=thread_id, body=body
            ).execute()
        _retry_gmail(_do, on_rebuild=self._rebuild_service)
