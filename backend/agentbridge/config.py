"""Runtime configuration for the AgentBridge server.

The server is launched from (or pointed at) the developer's repo — the "workspace".
All agent runs and git operations happen there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _default_workspace() -> Path:
    """Workspace defaults to AGENTBRIDGE_WORKSPACE, else the current working directory."""
    raw = os.environ.get("AGENTBRIDGE_WORKSPACE")
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def github_token() -> str | None:
    """Discover a GitHub token from the environment, then fall back to the gh CLI.

    Order: GITHUB_TOKEN -> GH_TOKEN -> `gh auth token`. Returns None if none found.

    Deliberately uncached: it's only consulted on PR creation (and the health check), so a fresh
    lookup is cheap, and caching would pin a stale token — e.g. one that appears after a `gh auth
    login` mid-session would never be picked up, and an expired one would linger as "valid".
    """
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if token := os.environ.get(var):
            return token.strip()

    if shutil.which("gh"):
        try:
            out = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
    return None


@dataclass
class Settings:
    workspace: Path = field(default_factory=_default_workspace)
    host: str = field(default_factory=lambda: os.environ.get("AGENTBRIDGE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("AGENTBRIDGE_PORT", "8000")))
    default_agent: str = field(default_factory=lambda: os.environ.get("AGENTBRIDGE_AGENT", "claude-code"))

    @property
    def github_token(self) -> str | None:
        return github_token()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
