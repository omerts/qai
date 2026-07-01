"""Cursor adapter command construction and stream-json parsing (no live CLI run)."""

import json
from pathlib import Path

from agentbridge.agents.cursor import CursorAdapter


def test_build_command_has_headless_flags():
    a = CursorAdapter(Path("."))
    cmd = a._build_command("do a thing")
    assert "--print" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--stream-partial-output" in cmd
    # Must not hang on approval/trust/MCP prompts in headless mode.
    assert "--force" in cmd and "--trust" in cmd and "--approve-mcps" in cmd
    # Prompt is positional after a `--` terminator.
    assert cmd[-2:] == ["--", "do a thing"]
    assert "--resume" not in cmd  # no chat id yet
    assert "--model" not in cmd   # default model


_LIST_MODELS_SAMPLE = """Available models

auto - Auto
gpt-5.3-codex - Codex 5.3
composer-2.5-fast - Composer 2.5 Fast (default)
claude-opus-4-8-high - Opus 4.8 1M
claude-sonnet-5-high - Sonnet 5 1M
gemini-3.1-pro - Gemini 3.1 Pro
"""


def test_parse_models_from_cli_output():
    parsed = CursorAdapter._parse_models(_LIST_MODELS_SAMPLE)
    ids = [m["id"] for m in parsed]
    # Header + blank lines skipped; every real id captured.
    assert "Available" not in " ".join(ids)
    assert ids == ["auto", "gpt-5.3-codex", "composer-2.5-fast",
                   "claude-opus-4-8-high", "claude-sonnet-5-high", "gemini-3.1-pro"]
    # "(default)" is stripped from the label.
    default_row = next(m for m in parsed if m["id"] == "composer-2.5-fast")
    assert default_row["label"] == "Composer 2.5 Fast"


def test_models_prepends_default_when_discovered(monkeypatch):
    monkeypatch.setattr(CursorAdapter, "_models_cache", None, raising=False)
    monkeypatch.setattr(CursorAdapter, "_discover_models",
                        classmethod(lambda cls: [{"id": "gpt-5.5-high", "label": "GPT-5.5"}]))
    models = CursorAdapter(Path(".")).models()
    assert models[0] == {"id": "", "label": "Default"}  # account default first
    assert {"id": "gpt-5.5-high", "label": "GPT-5.5"} in models


def test_models_empty_when_cli_absent(monkeypatch):
    monkeypatch.setattr(CursorAdapter, "_models_cache", None, raising=False)
    monkeypatch.setattr(CursorAdapter, "_discover_models", classmethod(lambda cls: []))
    assert CursorAdapter(Path(".")).models() == []  # no picker rather than bogus ids


def test_build_command_includes_selected_model():
    a = CursorAdapter(Path("."))
    a.set_model("gpt-5")
    cmd = a._build_command("hi")
    assert cmd[cmd.index("--model") + 1] == "gpt-5"
    a.set_model("")  # empty => back to default (no flag)
    assert "--model" not in a._build_command("hi")


def test_plan_mode_is_supported_and_flagged():
    a = CursorAdapter(Path("."))
    assert a.capabilities().plan_mode is True
    assert "--mode" not in a._build_command("hi")  # default: no plan flag
    a.set_mode("plan")
    cmd = a._build_command("hi")
    assert cmd[cmd.index("--mode") + 1] == "plan"
    a.set_mode("default")  # back to normal edit mode
    assert "--mode" not in a._build_command("hi")


def test_write_mcp_config_translates_and_merges(tmp_path):
    a = CursorAdapter(tmp_path)
    a._write_mcp_config({
        "figma": {"type": "http", "url": "https://mcp.figma.com/mcp", "headers": {"X": "1"}},
        "local": {"type": "stdio", "command": "npx", "args": ["-y", "srv"], "env": {"K": "v"}},
    })
    data = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    servers = data["mcpServers"]
    # Remote server -> url/headers, no `type` field (Cursor infers transport).
    assert servers["figma"] == {"url": "https://mcp.figma.com/mcp", "headers": {"X": "1"}}
    # Stdio server -> command/args/env.
    assert servers["local"] == {"command": "npx", "args": ["-y", "srv"], "env": {"K": "v"}}


def test_write_mcp_config_preserves_existing(tmp_path):
    path = tmp_path / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"mine": {"command": "keep"}}}))
    a = CursorAdapter(tmp_path)
    a._write_mcp_config({"added": {"type": "stdio", "command": "new"}})
    servers = json.loads(path.read_text())["mcpServers"]
    assert servers["mine"] == {"command": "keep"}  # untouched
    assert servers["added"] == {"command": "new"}


def test_write_mcp_config_noop_when_empty(tmp_path):
    a = CursorAdapter(tmp_path)
    a._write_mcp_config({})
    assert not (tmp_path / ".cursor").exists()


def test_claude_md_bridged_when_no_agents_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("Always run the tests.")
    assert CursorAdapter(tmp_path)._read_preamble() == "Always run the tests."


def test_claude_md_not_bridged_when_agents_md_present(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("guidance")
    (tmp_path / "AGENTS.md").write_text("native")
    assert CursorAdapter(tmp_path)._read_preamble() == ""  # Cursor reads AGENTS.md natively


def test_no_preamble_when_no_instructions(tmp_path):
    assert CursorAdapter(tmp_path)._read_preamble() == ""


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


def test_parse_success_result_is_skipped():
    a = CursorAdapter(Path("."))
    assert a._parse_line('{"type":"result","subtype":"success","result":"done"}') == []


def test_parse_error_result_is_surfaced():
    a = CursorAdapter(Path("."))
    events = a._parse_line('{"type":"result","subtype":"error","is_error":true,"result":"rate limited"}')
    assert len(events) == 1 and events[0].kind == "error" and "rate limited" in events[0].text


def test_parse_error_event_is_surfaced():
    a = CursorAdapter(Path("."))
    events = a._parse_line('{"type":"error","message":"model not found"}')
    assert events[0].kind == "error" and "model not found" in events[0].text
