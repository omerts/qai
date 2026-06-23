"""Per-workspace MCP server configuration.

Users connect plugins (e.g. Figma) by registering MCP servers in the widget. The set is
persisted per workspace in the state dir (next to chats) as ``mcp.json``, and the enabled
servers are handed to the Claude adapter as ``ClaudeAgentOptions.mcp_servers`` when a session
starts — in addition to any the workspace's own ``.mcp.json`` already provides.

A server is either:
- ``stdio`` — a local command (``command`` + ``args`` + ``env``), or
- ``http`` / ``sse`` — a remote endpoint (``url`` + ``headers``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .store import state_dir_for

_log = logging.getLogger("agentbridge")

#: Transports we support, matching the Claude Code / .mcp.json vocabulary.
TRANSPORTS = ("stdio", "http", "sse")


@dataclass
class McpServer:
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def is_valid(self) -> bool:
        if not self.name or self.transport not in TRANSPORTS:
            return False
        if self.transport == "stdio":
            return bool(self.command)
        return bool(self.url)

    def to_sdk(self) -> dict | None:
        """The config shape the claude-agent-sdk expects for this server, or None if invalid."""
        if not self.is_valid():
            return None
        if self.transport == "stdio":
            cfg: dict = {"type": "stdio", "command": self.command}
            if self.args:
                cfg["args"] = list(self.args)
            if self.env:
                cfg["env"] = dict(self.env)
            return cfg
        cfg = {"type": self.transport, "url": self.url}
        if self.headers:
            cfg["headers"] = dict(self.headers)
        return cfg


class McpStore:
    """Loads/saves the workspace's MCP servers as a single JSON file."""

    def __init__(self, workspace: Path) -> None:
        self.path = state_dir_for(workspace) / "mcp.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[McpServer]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            _log.warning("Ignoring unreadable MCP config %s: %s", self.path, exc)
            return []
        known = McpServer.__dataclass_fields__  # type: ignore[attr-defined]
        servers = []
        for item in data.get("servers", []) if isinstance(data, dict) else []:
            if isinstance(item, dict):
                servers.append(McpServer(**{k: v for k, v in item.items() if k in known}))
        return servers

    def _write(self, servers: list[McpServer]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"servers": [asdict(s) for s in servers]}, indent=2))
        tmp.replace(self.path)  # atomic on the same filesystem

    def save(self, server: McpServer) -> None:
        """Upsert by name (case-insensitive)."""
        servers = [s for s in self.list() if s.name.lower() != server.name.lower()]
        servers.append(server)
        self._write(servers)

    def delete(self, name: str) -> bool:
        servers = self.list()
        kept = [s for s in servers if s.name.lower() != name.lower()]
        if len(kept) == len(servers):
            return False
        self._write(kept)
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        servers = self.list()
        found = False
        for s in servers:
            if s.name.lower() == name.lower():
                s.enabled = enabled
                found = True
        if found:
            self._write(servers)
        return found

    def to_sdk(self) -> dict:
        """``{name: config}`` for every enabled, valid server — ready for ClaudeAgentOptions."""
        out: dict[str, dict] = {}
        for s in self.list():
            if not s.enabled:
                continue
            cfg = s.to_sdk()
            if cfg is not None:
                out[s.name] = cfg
        return out
