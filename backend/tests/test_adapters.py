import asyncio
import subprocess
from pathlib import Path

import pytest

from agentbridge import protocol as P
from agentbridge.agents.aider import AiderAdapter
from agentbridge.agents.base import AgentAdapter, AgentEvent, Capabilities, SessionContext
from agentbridge.agents.claude_code import ClaudeCodeAdapter
from agentbridge.agents.copilot import CopilotAdapter
from agentbridge.agents import registry
from agentbridge.sessions import Session
from agentbridge.store import ChatStore


async def _noop() -> None:
    return None


class _FakeMsg:
    """Minimal stand-in for an SDK init message (no SDK install needed)."""

    def __init__(self, subtype, data):
        self.subtype = subtype
        self.data = data


def test_mcp_status_events_flags_unconnected_servers(tmp_path: Path):
    adapter = ClaudeCodeAdapter(tmp_path)
    msg = _FakeMsg("init", {"mcp_servers": [
        {"name": "figma", "status": "failed"},
        {"name": "db", "status": "connected"},
    ]})
    events = adapter._mcp_status_events(msg)
    assert len(events) == 1
    assert "figma" in events[0].text and events[0].stream == "stderr"


def test_mcp_status_events_ignores_non_init_and_healthy(tmp_path: Path):
    adapter = ClaudeCodeAdapter(tmp_path)
    # Non-init messages are ignored entirely.
    assert adapter._mcp_status_events(_FakeMsg("assistant", {})) == []
    # All-connected init produces no warnings.
    healthy = _FakeMsg("init", {"mcp_servers": [{"name": "figma", "status": "connected"}]})
    assert adapter._mcp_status_events(healthy) == []


def test_set_mode_normalizes(tmp_path: Path):
    a = ClaudeCodeAdapter(tmp_path)
    assert a._mode == "default"
    a.set_mode("plan"); assert a._mode == "plan"
    a.set_mode("code"); assert a._mode == "default"      # widget alias for normal operation
    a.set_mode(None); assert a._mode == "default"
    a.set_mode("bogus"); assert a._mode == "default"     # unknown -> safe default
    a.set_mode("acceptEdits"); assert a._mode == "acceptEdits"


def test_claude_advertises_plan_mode(tmp_path: Path):
    assert ClaudeCodeAdapter(tmp_path).capabilities().plan_mode is True


def test_set_model_normalizes(tmp_path: Path):
    a = ClaudeCodeAdapter(tmp_path)
    assert a._model_id is None
    a.set_model("opus"); assert a._model_id == "opus"
    a.set_model("  sonnet "); assert a._model_id == "sonnet"
    a.set_model(""); assert a._model_id is None       # "Default" choice
    a.set_model(None); assert a._model_id is None


def test_claude_advertises_models(tmp_path: Path):
    ids = [m["id"] for m in ClaudeCodeAdapter(tmp_path).models()]
    assert ids[0] == "" and {"opus", "sonnet", "haiku"} <= set(ids)


def test_set_effort_normalizes(tmp_path: Path):
    a = ClaudeCodeAdapter(tmp_path)
    assert a._effort is None
    a.set_effort("high"); assert a._effort == "high"
    a.set_effort("HIGH"); assert a._effort == "high"
    a.set_effort(""); assert a._effort is None        # "Default" choice
    a.set_effort("bogus"); assert a._effort is None    # unknown -> default
    a.set_effort("max"); assert a._effort == "max"


def test_claude_advertises_efforts(tmp_path: Path):
    ids = [e["id"] for e in ClaudeCodeAdapter(tmp_path).efforts()]
    assert ids[0] == "" and {"low", "medium", "high", "max"} <= set(ids)


def test_prompt_event_carries_title():
    ev = AgentEvent.prompt("r1", "Pick a framework", options=["React", "Vue"], title="The agent is asking")
    assert ev.kind == "prompt" and ev.options == ["React", "Vue"] and ev.title == "The agent is asking"


def _make_session(tmp_path: Path, send, agent: str = "fake") -> Session:
    store = ChatStore(tmp_path)
    record = store.create(agent=agent, title="t")
    return Session(
        record=record,
        workspace=tmp_path,
        send=send,
        github_token=None,
        store=store,
        notify_chats=_noop,
        mcp_for_sdk=dict,
        live_mirror=lambda *a: _noop(),
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_format_context_resolves_element_source_to_repo_path(tmp_path: Path):
    # A selected element carries a browser-reported absolute path; the preamble should point the
    # agent at the real repo-relative file so it doesn't search.
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "StatusTabs.tsx").write_text("export const StatusTabs = () => null\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=tmp_path, check=True)

    session = _make_session(tmp_path, send=lambda m: _noop())
    ctx = {"element": {"label": "<div>", "source": {"file": "/build/host/src/StatusTabs.tsx", "line": 12}}}
    out = session._format_context(ctx)
    assert "Source file (open this first): src/StatusTabs.tsx:12" in out


def test_format_context_locates_file_by_component_when_no_source(tmp_path: Path):
    # React 19 case: no source path, only the component name -> still point at the file.
    _init_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "StatusTabs.tsx").write_text("export const StatusTabs = () => null\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=tmp_path, check=True)

    session = _make_session(tmp_path, send=lambda m: _noop())
    out = session._format_context({"element": {"label": "<div>", "component": "StatusTabs"}})
    assert "Source file (open this first): src/StatusTabs.tsx" in out


def test_format_context_resolves_route_to_page_file(tmp_path: Path):
    # The Next.js route is the most reliable pointer and also pins down the app's root dir.
    _init_repo(tmp_path)
    (tmp_path / "apps" / "dashboards" / "app" / "auth" / "login").mkdir(parents=True)
    (tmp_path / "apps" / "dashboards" / "app" / "auth" / "login" / "page.tsx").write_text("export default function Page(){return null}\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=tmp_path, check=True)

    session = _make_session(tmp_path, send=lambda m: _noop())
    out = session._format_context({"page": {"route": "/auth/login"}})
    assert "Route file (the page for this route): apps/dashboards/app/auth/login/page.tsx" in out


def test_format_context_skips_library_components_via_chain(tmp_path: Path):
    # Ant Design case: nearest component is a library internal ("Wave") not in the repo; the
    # chain carries the user's component, which is what we resolve and report as owning.
    _init_repo(tmp_path)
    (tmp_path / "app" / "auth" / "login").mkdir(parents=True)
    (tmp_path / "app" / "auth" / "login" / "LoginForm.tsx").write_text("export const LoginForm = () => null\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=tmp_path, check=True)

    session = _make_session(tmp_path, send=lambda m: _noop())
    out = session._format_context({"element": {
        "label": "<button>",
        "component": "Wave",
        "componentChain": ["Wave", "Button", "InternalButton", "LoginForm", "LoginPage"],
    }})
    assert "Source file (open this first): app/auth/login/LoginForm.tsx" in out
    assert "Owning component: LoginForm" in out  # not "Wave"


async def test_stub_adapters_unavailable_and_raise():
    assert AiderAdapter.is_available() is False
    assert CopilotAdapter.is_available() is False
    with pytest.raises(NotImplementedError):
        await AiderAdapter(Path(".")).start(SessionContext(session_id="s"))


class FakeAdapter(AgentAdapter):
    """An in-memory adapter that 'edits' a file so we can test the session flow."""

    name = "fake"
    label = "Fake"

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)

    @classmethod
    def is_available(cls) -> bool:
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def start(self, ctx: SessionContext) -> None:
        pass

    async def send(self, text: str):
        # Simulate the agent writing a file in the workspace.
        (self.workspace / "agent_made_this.txt").write_text("hello from agent\n")
        yield AgentEvent.chunk("Working on it...")
        yield AgentEvent.file("agent_made_this.txt")
        yield AgentEvent.done()

    async def stop(self) -> None:
        pass


async def test_session_edits_in_its_own_worktree(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTBRIDGE_WORKTREE_DIR", str(tmp_path.parent / "wt"))
    monkeypatch.setenv("AGENTBRIDGE_STATE_DIR", str(tmp_path.parent / "state"))
    _init_repo(tmp_path)
    monkeypatch.setitem(registry._ADAPTERS, "fake", FakeAdapter)
    sent: list[P.ServerMessage] = []

    async def send(m: P.ServerMessage) -> None:
        sent.append(m)

    session = _make_session(tmp_path, send)  # not live (direct session, no ChatHub)
    await session.handle(P.UserMessage(type="user_message", chat_id=session.chat_id, text="make a file"))

    types = [m.type for m in sent]
    assert "agent_chunk" in types and "file_changes" in types
    assert "branch_created" not in types
    # The agent edited its private worktree — the workspace is untouched (this session isn't live).
    assert session.worktree_path is not None
    assert (session.worktree_path / "agent_made_this.txt").exists()
    assert not (tmp_path / "agent_made_this.txt").exists()
    assert not session.repo_git.has_uncommitted_changes()
    assert session.repo_git.current_branch() == "main"
    kinds = [e["kind"] for e in session.record.transcript]
    assert "user" in kinds and "agent" in kinds


async def test_session_create_pr_from_worktree(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTBRIDGE_WORKTREE_DIR", str(tmp_path.parent / "wt"))
    monkeypatch.setenv("AGENTBRIDGE_STATE_DIR", str(tmp_path.parent / "state"))
    _init_repo(tmp_path)
    monkeypatch.setitem(registry._ADAPTERS, "fake", FakeAdapter)

    from agentbridge import git_service

    monkeypatch.setattr(git_service.GitService, "push", lambda self, *a, **k: None)
    monkeypatch.setattr(
        git_service.GitService,
        "create_pull_request",
        lambda self, **k: git_service.PullRequest(url="https://example/pull/1", number=1),
    )

    sent: list[P.ServerMessage] = []

    async def send(m: P.ServerMessage) -> None:
        sent.append(m)

    session = _make_session(tmp_path, send)
    await session.handle(P.UserMessage(type="user_message", chat_id=session.chat_id, text="make a file"))
    assert (session.worktree_path / "agent_made_this.txt").exists()

    await session.handle(P.CreatePR(type="create_pr", chat_id=session.chat_id, title="Add a file"))

    types = [m.type for m in sent]
    assert "branch_created" in types and "pr_created" in types
    # The change was committed on the chat's branch (in its worktree); the workspace is untouched.
    assert session.git.current_branch() == session.record.worktree_branch
    assert not session.git.has_uncommitted_changes()   # committed
    assert session.repo_git.current_branch() == "main"


def test_pr_meta_drops_agent_filler(tmp_path: Path, monkeypatch):
    """A chatty agent reply (filler lead-in + bulleted list) must not become the PR title, and the
    body must drop the 'Done. Here's a summary…' preamble while keeping the real content."""
    monkeypatch.setenv("AGENTBRIDGE_STATE_DIR", str(tmp_path.parent / "state"))
    _init_repo(tmp_path)
    session = _make_session(tmp_path, lambda m: _noop())
    session.record.transcript = [
        {"kind": "user", "text": "style inline code refs like the Figma node"},
        {"kind": "agent", "text": (
            "Done. Here's a summary of what changed:\n\n"
            "InlineCode component — a <code> element styled per Figma node 453:4180:\n\n"
            "Font: Inconsolata, monospace\n"
            "Background: rgba(227,225,231,0.10)\n"
        )},
    ]
    title, body = session._pr_meta(None, None, ["src/InlineCode.tsx"])
    # Title comes from the request, not the agent's "Done. Here's a summary…:" line.
    assert title == "Style inline code refs like the Figma node"
    assert "Done. Here's a summary" not in body and "Here's a summary" not in body
    assert "InlineCode component" in body          # real content kept
    assert "`src/InlineCode.tsx`" in body          # files listed
    assert "## Summary" in body and "## Files changed" in body

    # Configured PR notes are appended (before the footer) for derived bodies, and after a
    # user-typed body.
    _, body2 = session._pr_meta(None, None, ["src/InlineCode.tsx"], notes="Ticket: ABC-123")
    assert "Ticket: ABC-123" in body2
    assert body2.index("Ticket: ABC-123") < body2.index("_Opened via AgentBridge._")
    _, body3 = session._pr_meta("My title", "My own body.", [], notes="Ticket: ABC-123")
    assert body3 == "My own body.\n\nTicket: ABC-123"


async def test_session_pr_failure_preserves_changes_then_retry_succeeds(tmp_path: Path, monkeypatch):
    """A failed PR must NOT lose the agent's work: the change stays in the chat's worktree and a
    retry succeeds (even though the first attempt already committed before push failed)."""
    monkeypatch.setenv("AGENTBRIDGE_WORKTREE_DIR", str(tmp_path.parent / "wt"))
    monkeypatch.setenv("AGENTBRIDGE_STATE_DIR", str(tmp_path.parent / "state"))
    _init_repo(tmp_path)
    monkeypatch.setitem(registry._ADAPTERS, "fake", FakeAdapter)
    from agentbridge import git_service

    calls = {"push": 0}

    def flaky_push(self, *a, **k):
        calls["push"] += 1
        if calls["push"] == 1:
            raise git_service.GitError("push rejected")

    monkeypatch.setattr(git_service.GitService, "push", flaky_push)
    monkeypatch.setattr(
        git_service.GitService, "create_pull_request",
        lambda self, **k: git_service.PullRequest(url="https://example/pull/1", number=1),
    )

    sent: list[P.ServerMessage] = []

    async def send(m: P.ServerMessage) -> None:
        sent.append(m)

    session = _make_session(tmp_path, send)
    await session.handle(P.UserMessage(type="user_message", chat_id=session.chat_id, text="make a file"))
    wt_file = session.worktree_path / "agent_made_this.txt"
    assert wt_file.exists()

    # Attempt #1 fails — the change MUST still be in the worktree, with a commit ready to retry.
    await session.handle(P.CreatePR(type="create_pr", chat_id=session.chat_id, title="Add a file"))
    assert "pr_created" not in [m.type for m in sent]
    assert any(m.type == "error" for m in sent)
    assert wt_file.exists(), "a failed PR must not lose the agent's work"
    assert session._has_commits_ahead()   # committed locally, just not pushed

    # Attempt #2 succeeds.
    sent.clear()
    await session.handle(P.CreatePR(type="create_pr", chat_id=session.chat_id, title="Add a file"))
    assert "pr_created" in [m.type for m in sent]
