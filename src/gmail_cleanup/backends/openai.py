"""OpenAI-compatible backend. Works with:

  - real OpenAI (api.openai.com)
  - LM Studio (http://localhost:1234/v1)
  - llama.cpp server (http://localhost:8080/v1)
  - vLLM, Ollama's /v1 OpenAI shim, and anything else that speaks the
    OpenAI Chat Completions wire format.

Selected via GCA_BACKEND=openai. Configure via env:
  OPENAI_API_KEY      required for real OpenAI. For LM Studio / llama.cpp /
                      vLLM any non-empty placeholder is fine ("not-needed").
  OPENAI_BASE_URL     default https://api.openai.com/v1. Point at your
                      local server (e.g. http://localhost:1234/v1) for
                      LM Studio.
  OPENAI_MODEL        default gpt-4o-mini. For local servers, the exact
                      model name they advertise (LM Studio shows it in
                      the Server tab; llama.cpp uses the file name).
  OPENAI_MAX_TOKENS   default 4000
  OPENAI_TEMPERATURE  default 0.1
  OPENAI_JSON_MODE    default "1" — sends `response_format: json_object`.
                      Set to "0" if your local server doesn't support it
                      (older llama.cpp builds, some vLLM configs).
  OPENAI_DISABLE_THINKING default "0" — set to "1" when running against a
                      reasoning / thinking model (Qwen3, DeepSeek-R1, ...)
                      on llama.cpp. Adds
                      `extra_body.chat_template_kwargs.enable_thinking=false`
                      to each request so the model emits its answer directly
                      in `content` instead of burning the entire token
                      budget on `reasoning_content` chain-of-thought.
                      Real OpenAI ignores unknown extras; some other
                      OpenAI-compatible servers (vLLM, older LM Studio
                      builds) may reject them.
                      See docs/llama-server-setup.md for details.
  OPENAI_RETRIES      default 5 — retry transient connection / rate-limit
                      errors with exponential backoff (1, 2, 4, 8, 16 s).
"""

from __future__ import annotations

import logging
import os
import time


logger = logging.getLogger(__name__)


class OpenAIBackend:
    def __init__(self):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "Install the OpenAI SDK: `pip install openai`"
            ) from e
        api_key = os.environ.get("OPENAI_API_KEY") or "not-needed"
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        # max_retries=0 — let our explicit retry loop below own the
        # retry semantics so the logged attempt count is meaningful and
        # the user-tunable OPENAI_RETRIES is actually authoritative.
        self.client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", "4000"))
        self.temperature = float(os.environ.get("OPENAI_TEMPERATURE", "0.1"))
        self.json_mode = os.environ.get("OPENAI_JSON_MODE", "1") not in ("0", "false", "no")
        self.retries = int(os.environ.get("OPENAI_RETRIES", "5"))
        self.base_url = base_url

    def _is_retryable(self, exc: Exception) -> bool:
        """Connection-level or rate-limit errors that are worth retrying.
        Semantic errors (bad request, unsupported model, auth) are NOT
        retried — they'll just keep failing and we want to fail loud."""
        # Import lazily so the module can be imported without the openai
        # SDK installed (the constructor would have failed first anyway).
        from openai import APIConnectionError, APITimeoutError, RateLimitError, InternalServerError
        return isinstance(exc, (
            APIConnectionError,
            APITimeoutError,
            RateLimitError,
            InternalServerError,
        ))

    def classify_batch(self, prompt: str) -> str:
        kwargs = dict(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if self.json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        # Reasoning/thinking-mode models (Qwen3, DeepSeek-R1, etc.) on
        # llama.cpp servers split output between `reasoning_content`
        # (chain-of-thought) and `content` (final answer). When the model
        # spends its entire token budget on reasoning, `content` is empty
        # and the classifier sees "0 decisions returned." Send
        # `chat_template_kwargs.enable_thinking=false` as an OpenAI-API
        # extra body parameter to force the model straight to the answer.
        # Opt-in via env (default off) — real OpenAI ignores unknown
        # extras, but some other OpenAI-compatible servers (vLLM,
        # LM Studio older builds) may reject them.
        if os.environ.get("OPENAI_DISABLE_THINKING", "0") in ("1", "true", "yes"):
            kwargs["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": False}
            }

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
            except Exception as e:
                if not self._is_retryable(e) or attempt >= self.retries:
                    raise
                last_exc = e
                delay = min(2 ** attempt, 30)
                logger.warning(
                    "openai transient error (%s: %s) — retrying in %ds (attempt %d/%d)",
                    type(e).__name__, e, delay, attempt + 1, self.retries
                )
                time.sleep(delay)
        # Unreachable in practice — loop either returns or raises
        raise RuntimeError(
            f"openai call failed after {self.retries + 1} attempts"
        ) from last_exc

    def __repr__(self):
        return f"OpenAIBackend(model={self.model}, base_url={self.base_url})"
