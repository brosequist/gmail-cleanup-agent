"""gmail-cleanup-agent — LLM-driven Gmail triage."""
from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth is the installed package metadata, built from
    # pyproject.toml — the same version that gets git-tagged and released.
    # Deriving it here keeps __version__ from drifting from the release.
    __version__ = version("gmail-llm-cleanup")
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "0.0.0+unknown"
