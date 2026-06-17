"""Interactive permission round-trip for the Claude Code adapter.

Uses an injected fake client (no real SDK/CLI run) that calls the adapter's can_use_tool
callback mid-stream, exactly as ClaudeSDKClient does. Requires claude-code-sdk only for
its PermissionResult types, which the adapter returns from the callback.
"""

from pathlib import Path

import pytest

pytest.importorskip("claude_code_sdk")

from agentbridge.agents.base import SessionContext
from agentbridge.agents.claude_code import ClaudeCodeAdapter


class _Block:
    def __init__(self, text=None, thinking=None, name=None, input=None):
        if text is not None:
            self.text = text
        if thinking is not None:
            self.thinking = thinking
        if name is not None:
            self.name = name
            self.input = input or {}


class _Msg:
    def __init__(self, content):
        self.content = content


class FakeClient:
    """Mimics ClaudeSDKClient: streams text, asks permission, then edits if allowed."""

    def __init__(self, workspace, can_use_tool):
        self.can_use_tool = can_use_tool
        self.decision = None
        self.connected = False

    async def connect(self):
        self.connected = True

    async def query(self, text, session_id="default"):
        self._prompt = text

    async def receive_response(self):
        yield _Msg([_Block(text="On it — editing foo.py")])
        self.decision = await self.can_use_tool("Edit", {"file_path": "foo.py"}, None)
        if getattr(self.decision, "behavior", "") == "allow":
            yield _Msg([_Block(name="Edit", input={"file_path": "foo.py"})])

    async def disconnect(self):
        self.connected = False


async def _drive(answer: str):
    ClaudeCodeAdapter.client_factory = staticmethod(
        lambda workspace, can_use_tool, resume=None: FakeClient(workspace, can_use_tool)
    )
    try:
        adapter = ClaudeCodeAdapter(Path("."))
        await adapter.start(SessionContext(session_id="s"))
        events = []
        async for ev in adapter.send("please edit foo.py"):
            events.append(ev)
            if ev.kind == "prompt":
                await adapter.resolve_prompt(ev.request_id, answer)
        decision = adapter._client.decision
        await adapter.stop()
        return events, decision
    finally:
        ClaudeCodeAdapter.client_factory = None


async def test_interactive_allow_round_trip():
    events, decision = await _drive("Allow")
    kinds = [e.kind for e in events]
    assert "chunk" in kinds
    prompt = next(e for e in events if e.kind == "prompt")
    assert prompt.options == ["Allow", "Deny"]
    assert "foo.py" in prompt.text
    assert decision.behavior == "allow"
    # Allowing the edit produced a file_touched event.
    assert any(e.kind == "file_touched" and e.path == "foo.py" for e in events)
    assert events[-1].kind == "done"


async def test_interactive_deny_round_trip():
    events, decision = await _drive("Deny")
    assert decision.behavior == "deny"
    # No edit happened, so no file_touched event.
    assert not any(e.kind == "file_touched" for e in events)
    assert events[-1].kind == "done"


async def test_resolve_unknown_prompt_is_noop():
    adapter = ClaudeCodeAdapter(Path("."))
    await adapter.resolve_prompt("does-not-exist", "Allow")  # must not raise


def test_parser_tolerates_unknown_message_types():
    """The CLI emits new control messages (e.g. rate_limit_event) that the pinned SDK's
    parser doesn't recognize; we tolerate those instead of aborting the turn, but keep
    raising on genuine parse failures."""
    from agentbridge.agents.claude_code import _install_parser_tolerance
    from claude_code_sdk._internal import message_parser as mp
    from claude_code_sdk._errors import MessageParseError
    from claude_code_sdk.types import SystemMessage

    _install_parser_tolerance()

    msg = mp.parse_message({"type": "rate_limit_event", "foo": 1})
    assert isinstance(msg, SystemMessage)
    assert msg.subtype == "rate_limit_event"

    # A known type with a missing required field is a real error and must still raise.
    with pytest.raises(MessageParseError):
        mp.parse_message({"type": "result"})

    # A message with no type at all is also a real error.
    with pytest.raises(MessageParseError):
        mp.parse_message({"foo": 1})
