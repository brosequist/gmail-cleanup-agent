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
"""

from __future__ import annotations

import os


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
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.max_tokens = int(os.environ.get("OPENAI_MAX_TOKENS", "4000"))
        self.temperature = float(os.environ.get("OPENAI_TEMPERATURE", "0.1"))
        self.json_mode = os.environ.get("OPENAI_JSON_MODE", "1") not in ("0", "false", "no")
        self.base_url = base_url

    def classify_batch(self, prompt: str) -> str:
        kwargs = dict(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if self.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    def __repr__(self):
        return f"OpenAIBackend(model={self.model}, base_url={self.base_url})"
