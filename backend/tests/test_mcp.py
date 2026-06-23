"""Unit tests for the per-workspace MCP server store and its SDK conversion."""

from pathlib import Path

from agentbridge.mcp_config import McpServer, McpStore


def _store(tmp_path: Path, monkeypatch) -> McpStore:
    monkeypatch.setenv("AGENTBRIDGE_STATE_DIR", str(tmp_path / "state"))
    return McpStore(tmp_path)


def test_save_list_toggle_delete_roundtrip(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    assert store.list() == []

    store.save(McpServer(name="figma", transport="sse", url="http://127.0.0.1:3845/sse"))
    store.save(McpServer(name="db", transport="stdio", command="db-mcp", args=["--port", "5432"]))
    names = {s.name for s in store.list()}
    assert names == {"figma", "db"}

    # Save with the same name upserts rather than duplicating.
    store.save(McpServer(name="figma", transport="http", url="https://mcp.figma.com/mcp"))
    figma = [s for s in store.list() if s.name == "figma"][0]
    assert figma.transport == "http" and len([s for s in store.list() if s.name == "figma"]) == 1

    assert store.set_enabled("db", False) is True
    assert [s for s in store.list() if s.name == "db"][0].enabled is False
    assert store.set_enabled("missing", True) is False

    assert store.delete("db") is True
    assert store.delete("db") is False
    assert {s.name for s in store.list()} == {"figma"}


def test_to_sdk_only_enabled_and_valid(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.save(McpServer(name="figma", transport="sse", url="http://127.0.0.1:3845/sse"))
    store.save(McpServer(name="db", transport="stdio", command="db-mcp", env={"X": "1"}))
    store.save(McpServer(name="off", transport="stdio", command="x", enabled=False))
    store.save(McpServer(name="broken", transport="stdio"))  # no command -> dropped

    sdk = store.to_sdk()
    assert set(sdk) == {"figma", "db"}
    assert sdk["figma"] == {"type": "sse", "url": "http://127.0.0.1:3845/sse"}
    assert sdk["db"] == {"type": "stdio", "command": "db-mcp", "env": {"X": "1"}}


def test_validity_rules():
    assert McpServer(name="a", transport="stdio", command="x").is_valid()
    assert not McpServer(name="a", transport="stdio").is_valid()       # stdio needs a command
    assert McpServer(name="a", transport="http", url="http://x").is_valid()
    assert not McpServer(name="a", transport="http").is_valid()        # http needs a url
    assert not McpServer(name="", transport="stdio", command="x").is_valid()  # name required
