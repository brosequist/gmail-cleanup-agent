"""Claude API backend. Uses the Anthropic SDK; requires ANTHROPIC_API_KEY."""

from __future__ import annotations

import os


class ClaudeBackend:
    """Claude backend. Configure via env:
      ANTHROPIC_API_KEY   required
      CLAUDE_MODEL        default claude-haiku-4-5-20251001
      CLAUDE_MAX_TOKENS   default 4000
    """

    def __init__(self):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "Install anthropic: `pip install anthropic`"
            ) from e
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
        self.max_tokens = int(os.environ.get("CLAUDE_MAX_TOKENS", "4000"))

    def classify_batch(self, prompt: str) -> str:
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        # Concatenate all text blocks (Haiku usually returns one)
        return "".join(b.text for b in msg.content if hasattr(b, "text"))

    def __repr__(self):
        return f"ClaudeBackend(model={self.model})"
