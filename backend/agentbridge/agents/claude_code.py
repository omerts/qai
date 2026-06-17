"""Claude Code adapter, driven via the ``claude-code-sdk`` Python package.

Install with: ``pip install agentbridge[claude]`` (or ``pip install claude-code-sdk``).

This adapter uses :class:`ClaudeSDKClient` (a persistent, streaming session) rather than
the one-shot ``query()`` helper, for two reasons:

1. **Multi-turn continuity** — the same client/session is reused across chat turns.
2. **Interactive permissions** — we register a ``can_use_tool`` callback. When Claude wants
   to use a tool that needs approval (edit/write/bash/...), the callback surfaces an
   ``AgentEvent.prompt`` (with Allow/Deny options) to the frontend and *blocks* until the
   user answers, then returns the corresponding ``PermissionResult``. The answer is fed
   back via :meth:`resolve_prompt`.

Concurrency model: each ``send()`` turn runs the SDK in a background task that pushes
:class:`AgentEvent`s onto a queue; ``send()`` drains the queue. The ``can_use_tool``
callback pushes a prompt event then awaits a per-request :class:`asyncio.Future`, which
:meth:`resolve_prompt` resolves from a *different* task (the WS message handling the
user's answer) — so producer and waiter never share a task and cannot deadlock.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .base import AgentAdapter, AgentEvent, Capabilities, SessionContext

# Tools that should ask the user before running. Read-only tools are auto-approved by the
# SDK and never reach our callback; these are the ones worth a confirmation.
_CONFIRM_TOOLS = {"edit", "write", "multiedit", "str_replace", "notebookedit", "bash"}
_ALLOW_ANSWERS = {"allow", "yes", "y", "approve", "ok", ""}


def _sdk_installed() -> bool:
    return importlib.util.find_spec("claude_code_sdk") is not None


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude-code"
    label = "Claude Code"

    #: Overridable hook so tests can inject a fake client without the real SDK/CLI.
    client_factory: Callable[..., Any] | None = None

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self._client: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._queue: asyncio.Queue[AgentEvent] | None = None
        self._resume: str | None = None   # session id to resume from (set at start)
        self._session_id: str | None = None  # latest session id seen (for persistence)

    @classmethod
    def is_available(cls) -> bool:
        return cls.client_factory is not None or _sdk_installed()

    def capabilities(self) -> Capabilities:
        return Capabilities(streaming=True, interactive=True, edits_files=True)

    async def start(self, ctx: SessionContext) -> None:
        self._resume = ctx.resume
        self._session_id = ctx.resume
        self._client = self._make_client()
        await self._client.connect()

    def _make_client(self) -> Any:
        if self.client_factory is not None:
            return self.client_factory(
                workspace=self.workspace, can_use_tool=self._can_use_tool, resume=self._resume
            )

        if not _sdk_installed():
            raise RuntimeError("claude-code-sdk is not installed. Run: pip install claude-code-sdk")

        from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKClient  # type: ignore

        options = ClaudeCodeOptions(
            cwd=str(self.workspace),
            permission_mode="default",  # 'default' => edits/bash route through can_use_tool
            can_use_tool=self._can_use_tool,
            resume=self._resume,  # continue a prior conversation when reopening a chat
        )
        return ClaudeSDKClient(options=options)

    def resume_handle(self) -> str | None:
        return self._session_id

    # ------------------------------------------------------------------ #
    # Interactive permission callback
    # ------------------------------------------------------------------ #

    async def _can_use_tool(self, tool_name: str, tool_input: dict, context: Any):
        from claude_code_sdk import PermissionResultAllow, PermissionResultDeny  # type: ignore

        # Auto-approve anything not in the confirm set (the SDK rarely calls us for these,
        # but be defensive).
        if tool_name.lower() not in _CONFIRM_TOOLS:
            return PermissionResultAllow(updated_input=tool_input)

        answer = await self._ask(self._describe_tool(tool_name, tool_input), options=["Allow", "Deny"])
        if answer.strip().lower() in _ALLOW_ANSWERS:
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message=f"User declined the {tool_name} action.", interrupt=False)

    async def _ask(self, prompt: str, options: list[str] | None) -> str:
        """Surface a prompt to the frontend and block until :meth:`resolve_prompt` answers."""
        assert self._queue is not None, "_ask called outside of a turn"
        request_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        await self._queue.put(AgentEvent.prompt(request_id, prompt, options=options))
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def resolve_prompt(self, request_id: str, answer: str) -> None:
        future = self._pending.get(request_id)
        if future is not None and not future.done():
            future.set_result(answer)

    @staticmethod
    def _describe_tool(tool_name: str, tool_input: dict) -> str:
        target = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("command")
        if target:
            return f"Claude wants to run {tool_name} on: {target}"
        return f"Claude wants to use the {tool_name} tool."

    # ------------------------------------------------------------------ #
    # Turn loop
    # ------------------------------------------------------------------ #

    async def send(self, text: str) -> AsyncIterator[AgentEvent]:  # type: ignore[override]
        if self._client is None:
            yield AgentEvent.error("Claude Code session is not started.")
            return

        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._queue = queue
        done = object()

        async def run() -> None:
            try:
                await self._client.query(text)
                async for message in self._client.receive_response():
                    # Capture the session id so the chat can be resumed later.
                    sid = getattr(message, "session_id", None)
                    if sid:
                        self._session_id = sid
                    async for event in self._translate(message):
                        await queue.put(event)
            except Exception as exc:  # noqa: BLE001
                await queue.put(AgentEvent.error(f"Claude Code error: {exc}"))
            finally:
                await queue.put(done)  # type: ignore[arg-type]

        task = asyncio.create_task(run())
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                yield item  # type: ignore[misc]
        finally:
            await task
            self._queue = None
        yield AgentEvent.done()

    async def _translate(self, message) -> AsyncIterator[AgentEvent]:
        """Map one SDK message to zero or more AgentEvents (duck-typed across versions)."""
        content = getattr(message, "content", None)
        if content is None:
            return
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            # ThinkingBlock -> .thinking ; TextBlock -> .text
            thinking = getattr(block, "thinking", None)
            if thinking:
                yield AgentEvent.chunk(thinking, stream="thinking")
                continue
            text = getattr(block, "text", None)
            if text:
                yield AgentEvent.chunk(text)
                continue
            # ToolUseBlock -> .name / .input ; report file edits as touched files.
            name = (getattr(block, "name", "") or "").lower()
            tool_input = getattr(block, "input", None) or {}
            if name in {"edit", "write", "create", "multiedit", "str_replace", "notebookedit"}:
                path = tool_input.get("file_path") or tool_input.get("path")
                if path:
                    yield AgentEvent.file(str(path))

    async def stop(self) -> None:
        # Unblock any outstanding prompt so the turn task can finish.
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result("deny")
        self._pending.clear()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
