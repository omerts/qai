"""The adapter contract every coding agent integration implements.

``SessionManager`` talks only to :class:`AgentAdapter` and consumes :class:`AgentEvent`
streams, so it never needs to know whether an agent is driven via an SDK or a CLI
subprocess. Adapters never touch the WebSocket wire format — translation to protocol
messages happens in ``sessions.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Literal


@dataclass
class Capabilities:
    """Feature flags an adapter advertises to the frontend."""

    streaming: bool = True       # emits incremental output chunks
    interactive: bool = False    # can ask the user mid-run (AgentEvent.prompt)
    edits_files: bool = True     # makes changes to the workspace

    def as_dict(self) -> dict[str, bool]:
        return {"streaming": self.streaming, "interactive": self.interactive, "edits_files": self.edits_files}


@dataclass
class SessionContext:
    """Per-session info handed to an adapter at start time."""

    session_id: str
    title: str | None = None
    #: Agent-native handle to resume a prior conversation (Claude session id / Cursor chat
    #: id), or None to start fresh. Adapters that can't resume ignore it.
    resume: str | None = None


EventKind = Literal["chunk", "prompt", "file_touched", "done", "error"]


@dataclass
class AgentEvent:
    """A single thing an agent emitted during a turn.

    - ``chunk``:        incremental text output (use ``stream`` for stdout/stderr/thinking)
    - ``prompt``:       agent needs user input; carries ``request_id`` + ``text`` (the question)
    - ``file_touched``: a file path the agent created/modified (best-effort hint)
    - ``done``:         the turn finished
    - ``error``:        something failed; ``text`` is the message
    """

    kind: EventKind
    text: str = ""
    stream: Literal["stdout", "stderr", "thinking"] = "stdout"
    request_id: str | None = None
    path: str | None = None
    options: list[str] | None = None
    meta: dict = field(default_factory=dict)

    @classmethod
    def chunk(cls, text: str, stream: str = "stdout") -> "AgentEvent":
        return cls(kind="chunk", text=text, stream=stream)  # type: ignore[arg-type]

    @classmethod
    def prompt(cls, request_id: str, text: str, options: list[str] | None = None) -> "AgentEvent":
        return cls(kind="prompt", request_id=request_id, text=text, options=options)

    @classmethod
    def file(cls, path: str) -> "AgentEvent":
        return cls(kind="file_touched", path=path)

    @classmethod
    def done(cls) -> "AgentEvent":
        return cls(kind="done")

    @classmethod
    def error(cls, text: str) -> "AgentEvent":
        return cls(kind="error", text=text)


class AgentAdapter(ABC):
    """Uniform interface over a single coding agent."""

    #: stable identifier used on the wire (e.g. "claude-code", "cursor")
    name: str = "agent"
    #: human-friendly label for the agent picker
    label: str = "Agent"
    #: optional accent theming the widget applies when this agent is active. Keys map to
    #: CSS variables: ``accent`` -> ``--ab-accent``, ``accentFg`` -> ``--ab-accent-fg``.
    #: Empty (the default) keeps the widget's built-in accent.
    theme: dict[str, str] = {}

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Whether this agent's SDK/CLI is installed and usable on this machine."""

    @abstractmethod
    def capabilities(self) -> Capabilities:
        ...

    @abstractmethod
    async def start(self, ctx: SessionContext) -> None:
        """Prepare the agent for a session (spawn process / open SDK client)."""

    @abstractmethod
    def send(self, text: str) -> AsyncIterator[AgentEvent]:
        """Send one user turn and stream back the agent's events.

        Implemented as an ``async def`` generator (``async for ... yield``).
        """

    @abstractmethod
    async def stop(self) -> None:
        """Tear down the session (kill process / close client)."""

    async def resolve_prompt(self, request_id: str, answer: str) -> None:
        """Deliver a user's answer to an outstanding interactive prompt.

        Only meaningful for adapters whose ``capabilities().interactive`` is True (they
        emit ``AgentEvent.prompt`` mid-run and block until answered). The default is a
        no-op so non-interactive adapters need not implement it.
        """
        return None

    def resume_handle(self) -> str | None:
        """The agent-native id needed to resume this conversation later, if any.

        The session persists this so reopening the chat can continue with full context
        (passed back via :attr:`SessionContext.resume`). Default None = not resumable.
        """
        return None

    def set_auto_approve(self, enabled: bool) -> None:
        """Toggle auto-approval of tool actions (file edits / shell commands) for upcoming
        turns. When enabled, an interactive adapter should skip the Allow/Deny prompt for
        routine actions. Default is a no-op — headless adapters (e.g. Cursor) never prompt.
        """
        return None

    async def interrupt(self) -> bool:
        """Best-effort cancel of the in-flight turn. Returns True if the agent was interrupted,
        False if it doesn't support interruption (the turn then runs to completion). Default:
        not supported.
        """
        return False


class AgentUnavailableError(RuntimeError):
    """Raised when an adapter is selected but its backing tool is not installed."""
