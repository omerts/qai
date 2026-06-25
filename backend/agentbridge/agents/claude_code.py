"""Claude Code adapter, driven via the ``claude-agent-sdk`` Python package.

Install with: ``pip install agentbridge[claude]`` (or ``pip install claude-agent-sdk``).

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
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .base import AgentAdapter, AgentEvent, Capabilities, SessionContext

_log = logging.getLogger("agentbridge")

# Which settings the Claude Code CLI should load from disk. In ``--print`` (SDK) mode the CLI
# does NOT read filesystem settings unless told to, so we opt in explicitly: this is what
# makes the workspace's .claude/settings.json, .mcp.json, hooks, agents, and CLAUDE.md take
# effect (plus the developer's own user settings).
#
# We deliberately omit the ``local`` source (.claude/settings.local.json). That file is a
# per-user, machine-local permission cache the CLI *writes* to when it's an active source;
# excluding it keeps AgentBridge from reading or creating any .claude file in the user's
# workspace. Approvals here flow through the can_use_tool callback at runtime instead, so
# nothing is persisted to disk. Override with AGENTBRIDGE_CLAUDE_SETTING_SOURCES (a
# comma-separated subset of user,project,local) or set it empty to fall back to the CLI
# default — add ``local`` back only if you want the workspace's local settings to apply.
_DEFAULT_SETTING_SOURCES = "user,project"


def _setting_sources() -> list[str] | None:
    raw = os.environ.get("AGENTBRIDGE_CLAUDE_SETTING_SOURCES", _DEFAULT_SETTING_SOURCES).strip()
    if not raw:
        return None  # empty -> let the SDK use its default (loads no filesystem settings)
    return [s.strip() for s in raw.split(",") if s.strip()]


def _sandbox_enabled() -> bool:
    """Whether to keep Claude Code's OS-level bash sandbox on. Off by default: it relies on
    kernel primitives (bubblewrap/seccomp) that usually aren't available inside the AgentBridge
    container, where it blocks *all* Bash commands. The container itself is the isolation
    boundary, and AgentBridge already gates risky commands via the Allow/Deny flow. Re-enable
    with AGENTBRIDGE_CLAUDE_SANDBOX=1 on a host that supports it.
    """
    return os.environ.get("AGENTBRIDGE_CLAUDE_SANDBOX", "0").strip().lower() in ("1", "true", "yes", "on")

# Tools that should ask the user before running. Read-only tools are auto-approved by the
# SDK and never reach our callback; these are the ones worth a confirmation. ``exitplanmode`` is
# how the agent leaves plan mode — confirming it is the user approving the proposed plan.
_CONFIRM_TOOLS = {"edit", "write", "multiedit", "str_replace", "notebookedit", "bash", "exitplanmode"}
_ALLOW_ANSWERS = {"allow", "yes", "y", "approve", "ok", ""}

# SDK permission modes we accept from the widget (see ClaudeAgentOptions.permission_mode).
_VALID_MODES = {"default", "plan", "acceptEdits", "dontAsk", "bypassPermissions"}

# File-mutating tools — a tool_use of one of these means a file was touched.
_EDIT_TOOLS = {"edit", "write", "create", "multiedit", "str_replace", "notebookedit"}


def _trunc(s: object, n: int = 80) -> str:
    s = str(s).strip().replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_activity(name: str, tool_input: dict) -> str | None:
    """A short, human one-liner describing a tool call, shown as internal 'thinking' activity
    (it overwrites the previous one, so the bubble reads as the agent's current step)."""
    n = (name or "").lower()
    inp = tool_input or {}
    if n in _EDIT_TOOLS:
        p = inp.get("file_path") or inp.get("path")
        return f"Editing {_trunc(p)}" if p else "Editing a file"
    if n == "read":
        p = inp.get("file_path") or inp.get("path")
        return f"Reading {_trunc(p)}" if p else "Reading a file"
    if n in {"bash", "shell"}:
        return f"Running: {_trunc(inp.get('command', ''))}"
    if n in {"grep", "glob", "search"}:
        return f"Searching {_trunc(inp.get('pattern') or inp.get('query') or inp.get('path') or '')}"
    if n in {"task", "agent", "dispatch_agent"}:
        what = inp.get("description") or inp.get("subagent_type") or inp.get("prompt") or ""
        return f"Delegating: {_trunc(what)}"
    if n == "webfetch":
        return f"Fetching {_trunc(inp.get('url', ''))}"
    if n in {"todowrite", "todoread"}:
        return "Updating the plan"
    if not n:
        return None
    return f"Using {name}"

# When auto-approval is on we still pause for confirmation on shell commands that look
# destructive or hard to undo. Matched case-insensitively against the whole command line.
_RISKY_BASH_PATTERNS = [
    r"\brm\s+(?:-\w*\s+)*-?\w*r",        # rm -r / rm -rf / rm -fr (recursive delete)
    r"\bsudo\b",                          # privilege escalation
    r"\bmkfs(?:\.\w+)?\b",                # format a filesystem
    r"\bdd\b.*\bof=",                     # raw disk write
    r"\bof=/dev/",                        # writing to a device
    r">\s*/dev/(?:sd|nvme|disk|hd)",      # redirect into a block device
    r":\s*\(\s*\)\s*\{",                  # fork bomb :(){ :|:& };:
    r"\bchmod\s+(?:-\w*\s+)*-?\w*[rR]",   # recursive chmod
    r"\bchown\s+(?:-\w*\s+)*-?\w*[rR]",   # recursive chown
    r"(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh",  # curl … | sh (remote code exec)
    r"\bgit\s+push\b[^\n]*(?:--force\b|--force-with-lease\b|\s-f\b)",  # force push
    r"\bgit\s+reset\s+--hard\b",          # discard local work
    r"\bgit\s+clean\s+-\w*[fd]",          # delete untracked files
    r"\b(?:shutdown|reboot|halt|poweroff)\b",
    r"\bkillall\b|\bkill\s+-9\b",
    r">\s*/(?:etc|usr|bin|boot|sys|var)\b",  # clobbering a system path
]
_RISKY_BASH_RE = re.compile("|".join(_RISKY_BASH_PATTERNS), re.IGNORECASE)


def _is_risky_bash(command: str) -> bool:
    """True if a shell command looks destructive enough to confirm even under auto-approve."""
    return bool(command) and _RISKY_BASH_RE.search(command) is not None


def _sdk_installed() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


# Top-level message types the pinned SDK's parser understands. The Claude Code CLI keeps
# adding control/metadata messages (e.g. ``rate_limit_event``) that an older SDK doesn't
# know about; its parser raises ``MessageParseError("Unknown message type: ...")`` mid-stream,
# which would otherwise abort the whole turn. See :func:`_install_parser_tolerance`.
_KNOWN_MESSAGE_TYPES = {"user", "assistant", "system", "result", "stream_event"}
_parser_patched = False


def _install_parser_tolerance() -> None:
    """Make the SDK tolerate *unknown* top-level message types instead of raising.

    Unrecognized messages (new CLI control events) become a benign ``SystemMessage`` that our
    translator ignores. Genuine parse failures inside *known* message types still raise, so we
    don't mask real bugs. Idempotent; safe to call when the SDK isn't installed.
    """
    global _parser_patched
    if _parser_patched or not _sdk_installed():
        return

    from claude_agent_sdk._internal import message_parser as _mp  # type: ignore
    from claude_agent_sdk._errors import MessageParseError  # type: ignore
    from claude_agent_sdk.types import SystemMessage  # type: ignore

    _original_parse = _mp.parse_message

    def _tolerant_parse(data):
        try:
            return _original_parse(data)
        except MessageParseError:
            mt = data.get("type") if isinstance(data, dict) else None
            if isinstance(mt, str) and mt and mt not in _KNOWN_MESSAGE_TYPES:
                return SystemMessage(subtype=mt, data=data)
            raise

    _mp.parse_message = _tolerant_parse
    _parser_patched = True


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude-code"
    label = "Claude Code"
    theme = {"accent": "#d97757", "accentFg": "#ffffff"}  # Claude clay

    #: Overridable hook so tests can inject a fake client without the real SDK/CLI.
    client_factory: Callable[..., Any] | None = None

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self._client: Any = None
        self._pending: dict[str, asyncio.Future] = {}
        self._queue: asyncio.Queue[AgentEvent] | None = None
        self._resume: str | None = None   # session id to resume from (set at start)
        self._session_id: str | None = None  # latest session id seen (for persistence)
        self._auto_approve: bool = False   # when True, skip prompts for routine edits/commands
        self._interrupted: bool = False    # set while a user-requested stop is in flight
        self._mcp_servers: dict = {}       # user-registered MCP servers (plugins), {name: config}
        self._mode: str = "default"        # SDK permission_mode for the next turn ("plan" etc.)
        self._applied_mode: str | None = None  # last mode pushed to the live client

    @classmethod
    def is_available(cls) -> bool:
        return cls.client_factory is not None or _sdk_installed()

    def capabilities(self) -> Capabilities:
        return Capabilities(streaming=True, interactive=True, edits_files=True, plan_mode=True)

    async def start(self, ctx: SessionContext) -> None:
        self._resume = ctx.resume
        self._session_id = ctx.resume
        self._mcp_servers = ctx.mcp_servers or {}
        self._client = self._make_client()
        await self._client.connect()

    def _make_client(self) -> Any:
        if self.client_factory is not None:
            return self.client_factory(
                workspace=self.workspace, can_use_tool=self._can_use_tool, resume=self._resume
            )

        if not _sdk_installed():
            raise RuntimeError("claude-agent-sdk is not installed. Run: pip install claude-agent-sdk")

        _install_parser_tolerance()

        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # type: ignore

        options = ClaudeAgentOptions(
            cwd=str(self.workspace),
            permission_mode="default",  # 'default' => edits/bash route through can_use_tool
            can_use_tool=self._can_use_tool,
            resume=self._resume,  # continue a prior conversation when reopening a chat
            # Which settings to load from disk (see _setting_sources); defaults to user,project
            # so we never read or write the workspace's .claude/settings.local.json.
            setting_sources=_setting_sources(),
            # User-registered plugins (Figma, …). These merge with any the workspace's own
            # .mcp.json already provides. MCP tools route through can_use_tool like any other.
            mcp_servers=self._mcp_servers,
            # Disable the OS bash sandbox by default — it can't initialize in the container and
            # would block every Bash command (see _sandbox_enabled).
            sandbox={"enabled": _sandbox_enabled()},
        )
        # Make MCP wiring diagnosable: log which plugins we hand the SDK (names + transport only,
        # never the args/headers/env that may carry tokens).
        if self._mcp_servers:
            summary = {n: (c.get("type") or "stdio") for n, c in self._mcp_servers.items()}
            _log.info("Claude session starting with MCP servers: %s", summary)
        else:
            _log.info("Claude session starting with no user MCP servers configured.")
        return ClaudeSDKClient(options=options)

    def resume_handle(self) -> str | None:
        return self._session_id

    def set_auto_approve(self, enabled: bool) -> None:
        self._auto_approve = bool(enabled)

    def set_mode(self, mode: str | None) -> None:
        m = (mode or "default").strip()
        if m in ("", "code"):
            m = "default"
        self._mode = m if m in _VALID_MODES else "default"

    async def _apply_mode(self) -> None:
        """Push the selected permission mode to the live client before a turn. The SDK applies it
        immediately for subsequent tool requests; we only call on change."""
        if self._client is None or self._mode == self._applied_mode:
            return
        setter = getattr(self._client, "set_permission_mode", None)
        if setter is None:
            if self._mode != "default":
                _log.warning(
                    "Installed claude-agent-sdk has no set_permission_mode; '%s' mode ignored.",
                    self._mode,
                )
            return
        try:
            await setter(self._mode)
            self._applied_mode = self._mode
        except Exception as exc:  # noqa: BLE001
            _log.warning("Failed to set permission mode '%s': %s", self._mode, exc)

    async def interrupt(self) -> bool:
        """Stop the in-flight turn. The current receive loop then ends and the turn finishes."""
        if self._client is None:
            return False
        self._interrupted = True
        # Also unblock any pending Allow/Deny prompt so the turn isn't stuck awaiting an answer.
        self._cancel_pending()
        try:
            await self._client.interrupt()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _cancel_pending(self) -> None:
        """Resolve every outstanding Allow/Deny prompt as a denial and forget it, so any turn
        blocked in :meth:`_ask` can unwind. Used by both ``interrupt`` and ``stop`` so they stay
        consistent and never leave dead request ids in ``_pending``."""
        for future in self._pending.values():
            if not future.done():
                future.set_result("deny")
        self._pending.clear()

    async def summarize_pr(self, prompt: str) -> str | None:
        """Generate PR text with a one-shot ``query()`` — a fresh, isolated run that does NOT touch
        the live ClaudeSDKClient session (so the chat's context and resume id are untouched). Text
        only: no filesystem settings, no tools. Returns the model's reply, or None on any failure.
        """
        if not _sdk_installed():
            return None
        try:
            from claude_agent_sdk import ClaudeAgentOptions, query  # type: ignore
        except Exception:  # noqa: BLE001
            return None
        options = ClaudeAgentOptions(
            cwd=str(self.workspace),
            setting_sources=None,   # don't load project settings/MCP for a plain text generation
            allowed_tools=[],       # text only — the context is in the prompt
        )
        chunks: list[str] = []
        try:
            async for message in query(prompt=prompt, options=options):
                result = getattr(message, "result", None)
                if result and not getattr(message, "is_error", False):
                    chunks.append(str(result))
                    continue
                content = getattr(message, "content", None)
                if isinstance(content, list):
                    for block in content:
                        text = getattr(block, "text", None)
                        if text:
                            chunks.append(str(text))
        except Exception as exc:  # noqa: BLE001
            _log.warning("PR summary generation failed: %s", exc)
            return None
        return "".join(chunks).strip() or None

    # ------------------------------------------------------------------ #
    # Interactive permission callback
    # ------------------------------------------------------------------ #

    async def _can_use_tool(self, tool_name: str, tool_input: dict, context: Any):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny  # type: ignore

        name = tool_name.lower()

        # Auto-approve anything not in the confirm set (the SDK rarely calls us for these,
        # but be defensive).
        if name not in _CONFIRM_TOOLS:
            return PermissionResultAllow(updated_input=tool_input)

        # Auto-approve mode: edits run silently; shell commands run unless they look risky.
        if self._auto_approve:
            if name != "bash":
                return PermissionResultAllow(updated_input=tool_input)
            if not _is_risky_bash(str(tool_input.get("command", ""))):
                return PermissionResultAllow(updated_input=tool_input)
            # Risky command — fall through to ask the user anyway.

        answer = await self._ask(self._describe_tool(tool_name, tool_input), options=["Allow", "Deny"])
        if answer.strip().lower() in _ALLOW_ANSWERS:
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message=f"User declined the {tool_name} action.", interrupt=False)

    async def _ask(self, prompt: str, options: list[str] | None) -> str:
        """Surface a prompt to the frontend and block until :meth:`resolve_prompt` answers."""
        assert self._queue is not None, "_ask called outside of a turn"
        request_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
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
        if tool_name.lower() == "exitplanmode":
            plan = (tool_input or {}).get("plan") or ""
            header = "Claude finished planning. Approve to let it proceed with changes?"
            return f"{header}\n\n{plan}".strip()
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

        await self._apply_mode()  # honor the selected mode (e.g. plan) for this turn

        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._queue = queue
        self._interrupted = False
        done = object()

        async def run() -> None:
            try:
                await self._client.query(text)
                async for message in self._client.receive_response():
                    # Capture the session id so the chat can be resumed later.
                    sid = getattr(message, "session_id", None)
                    if sid:
                        self._session_id = sid
                    for event in self._mcp_status_events(message):
                        await queue.put(event)
                    async for event in self._translate(message):
                        await queue.put(event)
            except Exception as exc:  # noqa: BLE001
                # A user-requested interrupt may surface as an exception — that's expected,
                # not an error to report.
                if not self._interrupted:
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

    def _mcp_status_events(self, message) -> list[AgentEvent]:
        """The SDK's init message reports each MCP server's connection status. Log it and, for any
        server that didn't connect, surface a note so a misconfigured/unauthenticated plugin is
        visible instead of its tools just silently never appearing. Only acts on the init message;
        defensive against SDK shape changes."""
        if getattr(message, "subtype", None) != "init":
            return []
        data = getattr(message, "data", None) or {}
        servers = data.get("mcp_servers") if isinstance(data, dict) else None
        if not isinstance(servers, list) or not servers:
            return []
        _log.info("Claude session MCP server status: %s", servers)
        events: list[AgentEvent] = []
        for s in servers:
            if not isinstance(s, dict):
                continue
            status = str(s.get("status", "")).lower()
            if status and status not in ("connected", "ok", "ready"):
                events.append(AgentEvent.chunk(
                    f"⚠️ MCP server '{s.get('name')}' did not connect (status: {s.get('status')}). "
                    "Its tools won't be available this turn.\n",
                    stream="stderr",
                ))
        return events

    async def _translate(self, message) -> AsyncIterator[AgentEvent]:
        """Map one SDK message to zero or more AgentEvents (duck-typed across versions).

        The turn's *answer* is the final ``ResultMessage.result`` — that's the only thing sent
        as ``stdout``. Everything the agent does along the way (its reasoning, its running
        narration like "Let me search…", and its tool calls) goes to the ``thinking`` stream so
        it shows as transient progress in the thinking bubble, never mixed in with the answer.
        """
        content = getattr(message, "content", None)
        if content is None:
            # ResultMessage (no content block) — its `result` is the final answer.
            result = getattr(message, "result", None)
            if result and not getattr(message, "is_error", False):
                yield AgentEvent.chunk(result, stream="stdout")
            return
        blocks = content if isinstance(content, list) else [content]
        for block in blocks:
            # ThinkingBlock -> .thinking ; TextBlock -> .text. Both are progress, not answer.
            thinking = getattr(block, "thinking", None)
            if thinking:
                yield AgentEvent.chunk(thinking, stream="thinking")
                continue
            text = getattr(block, "text", None)
            if text:
                yield AgentEvent.chunk(text, stream="thinking")
                continue
            # ToolUseBlock -> .name / .input. Report file edits as touched files, and surface
            # every tool call as internal activity in the thinking bubble.
            name = getattr(block, "name", None)
            if name is None:
                continue  # e.g. ToolResultBlock — internal plumbing, nothing to show
            tool_input = getattr(block, "input", None) or {}
            if name.lower() in _EDIT_TOOLS:
                path = tool_input.get("file_path") or tool_input.get("path")
                if path:
                    yield AgentEvent.file(str(path))
            activity = _tool_activity(name, tool_input)
            if activity:
                yield AgentEvent.chunk(activity, stream="thinking")

    async def stop(self) -> None:
        # Unblock any outstanding prompt so the turn task can finish.
        self._cancel_pending()
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
