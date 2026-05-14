#!/usr/bin/env bash
# One-line entry point for the long-running cleanup pass.
# Resumable via state.json — run as many times as needed; already-classified
# threads are skipped automatically.
#
# Usage:
#   ./scripts/run-cleanup.sh             # default: dry-run, all matching threads
#   ./scripts/run-cleanup.sh --apply     # actually trash + label
#   ./scripts/run-cleanup.sh --limit 1000 --dry-run
#
# Any flags passed to this script are forwarded to `gmail_cleanup classify`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# shellcheck disable=SC1091
source .venv/bin/activate

export PYTHONPATH="${REPO_ROOT}/src"
export OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"

# Default args; user-passed args override / append
DEFAULT_ARGS=(
  --query "older_than:90d -has:userlabels -in:trash -in:spam"
  --concurrency 4
  --dry-run
)

# If user passed any flags, use those instead of defaults
if [[ $# -gt 0 ]]; then
  ARGS=("$@")
else
  ARGS=("${DEFAULT_ARGS[@]}")
fi

exec python -m gmail_cleanup classify "${ARGS[@]}" 2>&1 | tee -a dry-run.console.log
