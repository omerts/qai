import asyncio
import subprocess
from pathlib import Path

import pytest

from agentbridge import protocol as P
from agentbridge.agents.aider import AiderAdapter
from agentbridge.agents.base import AgentAdapter, AgentEvent, Capabilities, SessionContext
from agentbridge.agents.copilot import CopilotAdapter
from agentbridge.sessions import Session
from agentbridge.store import ChatStore


async def _noop() -> None:
    return None


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
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


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
