"""LLM backends — pluggable via GCA_BACKEND env var."""

import os

from .ollama import OllamaBackend


def get_backend():
    name = os.environ.get("GCA_BACKEND", "ollama").lower()
    if name == "ollama":
        return OllamaBackend()
    if name == "claude":
        from .claude import ClaudeBackend
        return ClaudeBackend()
    if name == "openai":
        from .openai import OpenAIBackend
        return OpenAIBackend()
    raise ValueError(f"Unknown GCA_BACKEND: {name!r}")
