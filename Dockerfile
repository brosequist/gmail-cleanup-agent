# Multi-stage build for the gmail-cleanup CLI.
# Final image is python:3.12-slim + the package + both optional extras
# (claude, openai), so any backend works out of the box.
#
# Usage:
#   docker run --rm -it \
#     -v "$PWD/config:/config" \
#     -v "$PWD:/work" -w /work \
#     -e GMAIL_CLEANUP_CONFIG_DIR=/config \
#     ghcr.io/brosequist/gmail-cleanup-agent:latest classify --dry-run

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel

FROM python:3.12-slim

LABEL org.opencontainers.image.title="gmail-cleanup-agent"
LABEL org.opencontainers.image.description="Triage a Gmail inbox with a local or cloud LLM"
LABEL org.opencontainers.image.source="https://github.com/brosequist/gmail-cleanup-agent"
LABEL org.opencontainers.image.licenses="MIT"

# Both extras pre-installed so the image works with every backend.
# Image stays small (~150 MB) because anthropic + openai are pure-Python.
#
# NOTE: `*.whl[claude,openai]` looks like pip-extras syntax but `[…]` is
# a shell glob character class, so the shell tries to match wheels
# ending in `c`/`l`/`a`/`u`/`d`/`e`/`,`/`o`/`p`/`n`/`i`. That always
# fails to expand and pip gets a literal `*.whl[claude,openai]`
# argument. Capture the wheel path first, then quote.
COPY --from=build /src/dist/*.whl /tmp/
RUN set -e; \
    whl=$(ls /tmp/*.whl); \
    pip install --no-cache-dir "${whl}[claude,openai]" && \
    rm -rf /tmp/*.whl

# Conventional mount points:
#   /config — credentials.json, token.json, labels.yaml, rules.md, etc.
#   /work   — host directory for state.json + log files
# Both are reset to non-root ownership at runtime via --user if needed.
ENV GMAIL_CLEANUP_CONFIG_DIR=/config \
    GMAIL_CLEANUP_WORK_DIR=/work
WORKDIR /work

ENTRYPOINT ["gmail-cleanup"]
CMD ["--help"]
