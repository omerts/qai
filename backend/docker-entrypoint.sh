#!/bin/sh
# Run the server as the host user (PUID/PGID) instead of root, so files the agent writes into
# the bind-mounted workspace are owned by *you* — otherwise they land root-owned and you can't
# edit them on the host (EACCES). Defaults to 1000:1000 (the typical first Linux user).
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
export HOME=/home/app

# Ensure PUID/PGID resolve to a real account whose home is /home/app. Without a passwd entry,
# tools that derive the home dir from getpwuid() (the Claude CLI, shells) fall back to "/" and
# fail trying to create /.claude. We set both $HOME and the passwd/group entries so every code
# path agrees on /home/app.
if ! getent group "$PGID" >/dev/null 2>&1; then
  echo "app:x:${PGID}:" >> /etc/group
fi
if ! getent passwd "$PUID" >/dev/null 2>&1; then
  echo "app:x:${PUID}:${PGID}:AgentBridge:/home/app:/bin/sh" >> /etc/passwd
fi
RUN_USER="$(getent passwd "$PUID" | cut -d: -f1)"
RUN_USER="${RUN_USER:-$PUID}"

# Named volumes mount root-owned; make the writable locations belong to the runtime user.
# (We intentionally don't recurse into $HOME so a read-only ~/.gitconfig mount is left alone.)
mkdir -p "$HOME/.claude" /worktrees /state
chown "$PUID:$PGID" "$HOME" 2>/dev/null || true
chown -R "$PUID:$PGID" "$HOME/.claude" /worktrees /state 2>/dev/null || true

# Trust any repo dir regardless of owner (the workspace's owner differs from the container's).
git config --system --add safe.directory '*' 2>/dev/null || true

# Drop privileges to the host user (by name so gosu picks up its home) and start the server.
exec gosu "$RUN_USER" agentbridge
