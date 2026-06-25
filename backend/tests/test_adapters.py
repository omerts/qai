import asyncio
import subprocess
from pathlib import Path

import pytest

from agentbridge import protocol as P
from agentbridge.agents.aider import AiderAdapter
from agentbridge.agents.base import AgentAdapter, AgentEvent, Capabilities, SessionContext
from agentbridge.agents.claude_code import ClaudeCodeAdapter
from agentbridge.agents.copilot import CopilotAdapter
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


def _make_session(tmp_path: Path, send, agent: str = "fake") -> Session:
    store = ChatStore(tmp_path)
    record = store.create(agent=agent, title="t")
    return Session(
        record=record,
        workspace=tmp_path,
        send=send,
        github_token=None,
        store=store,
        turn_lock=asyncio.Lock(),
        notify_chats=_noop,
        mcp_for_sdk=dict,
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


async def test_session_edits_in_place_without_branching(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTBRIDGE_STATE_DIR", str(tmp_path.parent / "state"))
    _init_repo(tmp_path)
    sent: list[P.ServerMessage] = []

    async def send(m: P.ServerMessage) -> None:
        sent.append(m)

    session = _make_session(tmp_path, send)
    session.adapter = FakeAdapter(tmp_path)  # bypass registry; pretend the adapter started

    await session.handle(P.UserMessage(type="user_message", chat_id=session.chat_id, text="make a file"))

    types = [m.type for m in sent]
    assert "agent_chunk" in types
    assert "file_changes" in types
    # The agent edits in place on the current branch — no branch suggestion or auto-branching.
    assert "branch_suggested" not in types
    assert "branch_created" not in types
    assert session.git.current_branch() == "main"
    assert (tmp_path / "agent_made_this.txt").exists()  # edit is live in the workspace
    # The turn was persisted to the transcript.
    kinds = [e["kind"] for e in session.record.transcript]
    assert "user" in kinds and "agent" in kinds


async def test_session_create_pr_resets_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTBRIDGE_WORKTREE_DIR", str(tmp_path.parent / "wt"))
    monkeypatch.setenv("AGENTBRIDGE_STATE_DIR", str(tmp_path.parent / "state"))
    _init_repo(tmp_path)

    # Don't hit the network: stub push + the GitHub PR call.
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
    session.adapter = FakeAdapter(tmp_path)
    await session.handle(P.UserMessage(type="user_message", chat_id=session.chat_id, text="make a file"))
    assert (tmp_path / "agent_made_this.txt").exists()

    await session.handle(P.CreatePR(type="create_pr", chat_id=session.chat_id, title="Add a file"))

    types = [m.type for m in sent]
    assert "branch_created" in types and "pr_created" in types
    # The edits were relocated onto the branch worktree and the workspace was reset clean.
    assert not (tmp_path / "agent_made_this.txt").exists()
    assert session.git.current_branch() == "main"
    assert not session.git.has_uncommitted_changes()
