import subprocess
from pathlib import Path

import pytest

from agentbridge import protocol as P
from agentbridge.agents.aider import AiderAdapter
from agentbridge.agents.base import AgentAdapter, AgentEvent, Capabilities, SessionContext
from agentbridge.agents.copilot import CopilotAdapter
from agentbridge.sessions import Session


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


async def test_session_suggests_branch_on_first_edit(tmp_path: Path):
    _init_repo(tmp_path)
    sent: list[P.ServerMessage] = []

    async def send(m: P.ServerMessage) -> None:
        sent.append(m)

    session = Session(tmp_path, send, github_token=None)
    session.adapter = FakeAdapter(tmp_path)  # bypass registry; pretend session started

    await session.handle(P.UserMessage(type="user_message", text="make a file"))

    types = [m.type for m in sent]
    assert "agent_chunk" in types
    assert "branch_suggested" in types  # advisory branch suggestion fired
    assert "file_changes" in types
    # The suggestion must NOT have auto-created a branch.
    assert session.git.current_branch() == "main"
    assert "branch_created" not in types


async def test_session_create_branch_is_user_triggered(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENTBRIDGE_WORKTREE_DIR", str(tmp_path.parent / "wt"))
    _init_repo(tmp_path)
    sent: list[P.ServerMessage] = []

    async def send(m: P.ServerMessage) -> None:
        sent.append(m)

    session = Session(tmp_path, send, github_token=None)
    await session.handle(P.CreateBranch(type="create_branch", name="agentbridge/manual"))

    # Choosing a branch does NOT switch the workspace branch or create anything yet — the
    # branch is recorded and materialized as a worktree only at PR time.
    assert session.git.current_branch() == "main"
    assert session.target_branch == "agentbridge/manual"
    created = [m for m in sent if m.type == "branch_created"]
    assert created and created[0].branch == "agentbridge/manual"
    assert created[0].worktree_path is None
