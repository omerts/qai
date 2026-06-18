#!/bin/sh
# Run the server as the host user (PUID/PGID) instead of root, so files the agent writes into
# the bind-mounted workspace are owned by *you* — otherwise they land root-owned and you can't
# edit them on the host (EACCES). Defaults to 1000:1000 (the typical first Linux user).
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
export HOME=/home/app

# Named volumes mount root-owned; make the writable locations belong to the runtime user.
# (We intentionally don't recurse into $HOME so a read-only ~/.gitconfig mount is left alone.)
mkdir -p "$HOME/.claude" /worktrees /state
chown "$PUID:$PGID" "$HOME" 2>/dev/null || true
chown -R "$PUID:$PGID" "$HOME/.claude" /worktrees /state 2>/dev/null || true

# Trust any repo dir regardless of owner (the workspace's owner differs from the container's).
git config --system --add safe.directory '*' 2>/dev/null || true

# Drop privileges to the host user and start the server.
exec gosu "$PUID:$PGID" agentbridge
