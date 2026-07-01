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
# - openssh-client: fallback for ssh:// git remotes (token HTTPS push is preferred)
# - nodejs/npm: required by the Claude Code CLI that claude-agent-sdk spawns
# - gosu: drop from root to the host user at startup (see docker-entrypoint.sh)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates openssh-client gosu nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Home for the non-root runtime user the entrypoint drops to (HOME=/home/app).
RUN mkdir -p /home/app/.claude

# The workspace is a bind mount owned by the host user, so Git would reject it with
# "detected dubious ownership". Trust any repo dir inside the container (single-tenant).
RUN git config --system --add safe.directory '*'

# Claude Code CLI (the binary the SDK shells out to). Authenticate at runtime with
# ANTHROPIC_API_KEY. Pinned to the 2.x line to stay protocol-compatible with the
# claude-agent-sdk pinned in backend/pyproject.toml — the SDK and CLI must move together
# (a mismatched pair fails every permission check with a Zod validation error).
RUN npm install -g @anthropic-ai/claude-code@2 && npm cache clean --force

# Cursor CLI (cursor-agent), so the "cursor" agent works headless — authenticate at runtime with
# CURSOR_API_KEY (see docker-compose.yml). cursor-agent is a wrapper that needs its bundled node +
# JS siblings, so keep the whole install tree (installed under /opt/cursor via HOME) world-readable
# and symlink it onto the PATH — the container runs as the non-root app user. `--version` verifies
# the install (as root and, implicitly via a+rX, for the app user), failing the build if it broke.
RUN curl https://cursor.com/install -fsSL | HOME=/opt/cursor bash \
    && chmod -R a+rX /opt/cursor \
    && ln -sf /opt/cursor/.local/bin/cursor-agent /usr/local/bin/cursor-agent \
    && cursor-agent --version

WORKDIR /app

# Install Python deps against ONLY the dependency metadata so this expensive layer is cached
# and reused across source edits. A stub package lets the editable install resolve; the real
# source is copied afterward. Editable installs are path-based, so the real code is picked up
# at runtime — and because the pip layer no longer depends on the source, editing
# backend/agentbridge no longer reinstalls every dependency.
COPY backend/pyproject.toml backend/README.md ./backend/
RUN mkdir -p ./backend/agentbridge \
    && touch ./backend/agentbridge/__init__.py \
    && pip install --no-cache-dir -e "./backend[claude]"

# Real source — edits here only bust this layer, not the dependency install above.
COPY backend/agentbridge ./backend/agentbridge

# The built widget bundle, served by the backend at /widget. Mirrors the repo layout so
# main.py's relative lookup (../../widget/dist) finds it.
COPY widget/dist ./widget/dist

# Bind to all interfaces inside the container; the workspace is the mounted repo. HOME points
# at the non-root runtime user's home so the Claude CLI's user settings/session store land on
# the mounted volume (see docker-compose.yml).
ENV AGENTBRIDGE_HOST=0.0.0.0 \
    AGENTBRIDGE_PORT=8000 \
    AGENTBRIDGE_WORKSPACE=/workspace \
    HOME=/home/app

EXPOSE 8000

# The entrypoint drops from root to the host user (PUID/PGID) so the agent's edits to the
# bind-mounted workspace are owned by you, not root.
COPY backend/docker-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
