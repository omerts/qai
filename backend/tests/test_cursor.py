"""Cursor adapter command construction and stream-json parsing (no live CLI run)."""

from pathlib import Path

from agentbridge.agents.cursor import CursorAdapter


def test_build_command_has_headless_flags():
    a = CursorAdapter(Path("."))
    cmd = a._build_command("do a thing")
    assert "--print" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--stream-partial-output" in cmd
    # Must not hang on approval/trust prompts in headless mode.
    assert "--force" in cmd and "--trust" in cmd
    # Prompt is positional after a `--` terminator.
    assert cmd[-2:] == ["--", "do a thing"]
    assert "--resume" not in cmd  # no chat id yet


def test_build_command_resumes_with_chat_id():
    a = CursorAdapter(Path("."))
    a._chat_id = "chat_123"
    cmd = a._build_command("next turn")
    assert cmd[cmd.index("--resume") + 1] == "chat_123"


def test_parse_assistant_message_text():
    a = CursorAdapter(Path("."))
    line = '{"type":"assistant","message":{"content":[{"type":"text","text":"hello"}]}}'
    events = a._parse_line(line)
    assert len(events) == 1 and events[0].kind == "chunk" and events[0].text == "hello"


def test_parse_text_delta():
    a = CursorAdapter(Path("."))
    events = a._parse_line('{"type":"assistant","text":"partial"}')
    assert events[0].text == "partial"


def test_parse_captures_session_id_and_skips_result():
    a = CursorAdapter(Path("."))
    events = a._parse_line('{"type":"result","subtype":"success","result":"done","session_id":"sess_9"}')
    assert events == []  # result is not re-emitted (would duplicate streamed text)
    assert a._chat_id == "sess_9"  # captured for resuming


def test_parse_file_tool_event():
    a = CursorAdapter(Path("."))
    events = a._parse_line('{"type":"tool_call","name":"edit","args":{"path":"src/app.py"}}')
    assert any(e.kind == "file_touched" and e.path == "src/app.py" for e in events)


def test_parse_non_json_line_is_emitted_verbatim():
    a = CursorAdapter(Path("."))
    events = a._parse_line("plain text output")
    assert events[0].kind == "chunk" and "plain text output" in events[0].text
