# AgentBridge backend image.
#
# Ships the FastAPI server, git, and the Claude Code CLI (the agent the backend drives via
# claude-agent-sdk). The repo you want the agent to edit is mounted at /workspace at run
# time — see docker-compose.yml.
#
# NOTE: build context is the REPOSITORY ROOT (not backend/), so the widget bundle can be
# included. With docker-compose this is handled for you. Manual build:
#   docker build -f backend/Dockerfile -t agentbridge .
FROM python:3.12-slim

# - git: all branch/commit/push operations run against the mounted workspace
# - curl/ca-certificates: TLS + tooling
# - nodejs/npm: required by the Claude Code CLI that claude-agent-sdk spawns
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# The workspace is a bind mount owned by the host user, so Git would reject it with
# "detected dubious ownership". Trust any repo dir inside the container (single-tenant).
RUN git config --system --add safe.directory '*'

# Claude Code CLI (the binary the SDK shells out to). Authenticate at runtime with
# ANTHROPIC_API_KEY. Pinned to the 2.x line to stay protocol-compatible with the
# claude-agent-sdk pinned in backend/pyproject.toml — the SDK and CLI must move together
# (a mismatched pair fails every permission check with a Zod validation error).
RUN npm install -g @anthropic-ai/claude-code@2 && npm cache clean --force

WORKDIR /app

# Install Python deps first for better layer caching: copy only dependency metadata + the
# package, then install editable so the source layout (and the sibling widget/dist that the
# server serves) resolves at runtime exactly as in development.
COPY backend/pyproject.toml backend/README.md ./backend/
COPY backend/agentbridge ./backend/agentbridge
RUN pip install --no-cache-dir -e "./backend[claude]"

# The built widget bundle, served by the backend at /widget. Mirrors the repo layout so
# main.py's relative lookup (../../widget/dist) finds it.
COPY widget/dist ./widget/dist

# Bind to all interfaces inside the container; the workspace is the mounted repo.
ENV AGENTBRIDGE_HOST=0.0.0.0 \
    AGENTBRIDGE_PORT=8000 \
    AGENTBRIDGE_WORKSPACE=/workspace

EXPOSE 8000

# Mark the bind-mounted workspace as a safe git directory (its ownership usually differs
# from the container user), then launch the server.
RUN printf '#!/bin/sh\nset -e\ngit config --global --add safe.directory "${AGENTBRIDGE_WORKSPACE}" 2>/dev/null || true\nexec agentbridge\n' > /usr/local/bin/entrypoint.sh \
    && chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
