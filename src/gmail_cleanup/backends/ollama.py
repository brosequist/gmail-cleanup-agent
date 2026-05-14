"""Ollama backend. Calls /api/chat with `format: json` so the model is
constrained to emit JSON output."""

from __future__ import annotations

import logging
import os
import time

import httpx


logger = logging.getLogger(__name__)


# Errors worth retrying — transient network issues from `kubectl port-forward`
# dying mid-stream, or from intermittent server-side hiccups. We do NOT
# retry on HTTPStatusError (semantic errors should fail loud).
_RETRYABLE_EXCEPTIONS = (
    httpx.RemoteProtocolError,    # peer closed connection mid-response
    httpx.ConnectError,            # couldn't establish connection
    httpx.ReadError,               # read failed mid-stream
    httpx.WriteError,              # write failed
    httpx.ConnectTimeout,          # connect timeout
    httpx.ReadTimeout,             # read timeout (rare for fast LLM)
    OSError,                       # ECONNRESET, EPIPE etc. surface as OSError
)


class OllamaBackend:
    """Ollama backend. Configure via env:
      OLLAMA_HOST       default http://localhost:11434
      OLLAMA_MODEL      default qwen3.6:35b-a3b-iq3_xxs-fixed
      OLLAMA_NUM_CTX    default 8192
      OLLAMA_KEEP_ALIVE default 60m (keep model warm between batches)
      OLLAMA_TIMEOUT    default 300 (per-call read timeout, seconds)
      OLLAMA_RETRIES    default 5  (transient retries on connection errors)
    """

    def __init__(self):
        self.host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        self.model = os.environ.get("OLLAMA_MODEL", "qwen3.6:35b-a3b-iq3_xxs-fixed")
        self.num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))
        self.keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", "60m")
        self.timeout = float(os.environ.get("OLLAMA_TIMEOUT", "300"))
        self.retries = int(os.environ.get("OLLAMA_RETRIES", "5"))
        # Single httpx client reused across calls — keepalive saves the
        # TCP+TLS handshake, and means a `kubectl port-forward` blip
        # surfaces as a single retryable error rather than a slow stream
        # of fresh-connection failures.
        self._client = httpx.Client(timeout=self.timeout, http2=False)

    def classify_batch(self, prompt: str) -> str:
        """Send the prompt to Ollama, return the raw `message.content`
        string. Retries up to OLLAMA_RETRIES times on transient network
        errors (connection reset, timeout, peer closed)."""
        body = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "keep_alive": self.keep_alive,
            "messages": [{"role": "user", "content": prompt}],
            "options": {
                "temperature": 0.1,
                "num_ctx": self.num_ctx,
            },
        }
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                r = self._client.post(f"{self.host}/api/chat", json=body)
                r.raise_for_status()
                data = r.json()
                return data.get("message", {}).get("content", "")
            except _RETRYABLE_EXCEPTIONS as e:
                last_exc = e
                if attempt < self.retries:
                    # Exponential backoff with a small floor + ceiling so
                    # a flapping port-forward doesn't get retried for hours.
                    delay = min(2 ** attempt, 30)
                    logger.warning(
                        "ollama transient error (%s: %s) — retrying in %ds (attempt %d/%d)",
                        type(e).__name__, e, delay, attempt + 1, self.retries
                    )
                    # Re-create the client on socket-level errors — the
                    # existing connection-pool entry may be poisoned.
                    self._client.close()
                    self._client = httpx.Client(timeout=self.timeout, http2=False)
                    time.sleep(delay)
                    continue
                break
        # Out of retries — surface to caller with the original exception
        raise RuntimeError(
            f"ollama call failed after {self.retries + 1} attempts: "
            f"{type(last_exc).__name__}: {last_exc}"
        ) from last_exc

    def close(self):
        try:
            self._client.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

    def __repr__(self):
        return f"OllamaBackend(model={self.model}, host={self.host})"
