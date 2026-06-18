"""Registry mapping agent names to adapter classes.

Add a new agent by importing its class and listing it in ``_ADAPTERS``.
"""

from __future__ import annotations

from pathlib import Path

from ..protocol import AgentInfo
from .aider import AiderAdapter
from .base import AgentAdapter
from .claude_code import ClaudeCodeAdapter
from .copilot import CopilotAdapter
from .cursor import CursorAdapter

_ADAPTERS: dict[str, type[AgentAdapter]] = {
    ClaudeCodeAdapter.name: ClaudeCodeAdapter,
    CursorAdapter.name: CursorAdapter,
    AiderAdapter.name: AiderAdapter,
    CopilotAdapter.name: CopilotAdapter,
}


def adapter_names() -> list[str]:
    return list(_ADAPTERS)


def get_adapter_class(name: str) -> type[AgentAdapter]:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise KeyError(f"Unknown agent '{name}'. Known agents: {', '.join(_ADAPTERS)}") from None


def create_adapter(name: str, workspace: Path) -> AgentAdapter:
    return get_adapter_class(name)(workspace)


def list_agent_info() -> list[AgentInfo]:
    """Describe every registered agent for the frontend picker."""
    infos: list[AgentInfo] = []
    for name, cls in _ADAPTERS.items():
        available = cls.is_available()
        # capabilities() is an instance method; adapters are cheap to construct.
        try:
            caps = cls(Path(".")).capabilities().as_dict()
        except Exception:  # noqa: BLE001 — never let one adapter break the list
            caps = {}
        infos.append(
            AgentInfo(
                name=name,
                label=cls.label,
                available=available,
                capabilities=caps,
                theme=dict(cls.theme),
            )
        )
    return infos
