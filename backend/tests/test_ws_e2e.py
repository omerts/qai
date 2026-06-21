"""End-to-end test through the real WebSocket endpoint in main.py.

Registers an in-memory FakeAdapter, points the server at a temp git repo, and drives the
full flow: list_agents -> start_session -> user_message (agent edits a file) ->
create_pr (GitHub mocked), committing only the agent's files.
"""

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentbridge import config, git_service
from agentbridge.agents import registry
from agentbridge.agents.base import AgentAdapter, AgentEvent, Capabilities, SessionContext


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/widgets.git"], cwd=path, check=True)
    (path / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


class FakeAdapter(AgentAdapter):
    name = "fake"
    label = "Fake"

    @classmethod
    def is_available(cls) -> bool:
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def start(self, ctx: SessionContext) -> None:
        pass

    async def send(self, text: str):
        (self.workspace / "feature.txt").write_text("new feature\n")
        yield AgentEvent.chunk("Added feature.txt")
        yield AgentEvent.file("feature.txt")
        yield AgentEvent.done()

    async def stop(self) -> None:
        pass


class InteractiveFakeAdapter(AgentAdapter):
    """Emits a prompt and blocks until resolve_prompt is called — exercises the
    concurrent message-handling path (agent_response received mid-turn)."""

    name = "ifake"
    label = "Interactive Fake"

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self._future = None

    @classmethod
    def is_available(cls) -> bool:
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities(interactive=True)

    async def start(self, ctx: SessionContext) -> None:
        pass

    async def send(self, text: str):
        self._future = asyncio.get_event_loop().create_future()
        yield AgentEvent.prompt("req-1", "Allow edit to app.py?", options=["Allow", "Deny"])
        answer = await self._future
        if answer == "Allow":
            (self.workspace / "app.py").write_text("# edited\n")
            yield AgentEvent.file("app.py")
        yield AgentEvent.done()

    async def resolve_prompt(self, request_id: str, answer: str) -> None:
        if self._future is not None and not self._future.done():
            self._future.set_result(answer)

    async def stop(self) -> None:
        pass


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    _init_repo(tmp_path)
    # Point the server at the temp repo and give it a token.
    monkeypatch.setenv("AGENTBRIDGE_WORKSPACE", str(tmp_path))
    # Keep agent worktrees out of the repo (and out of other tests' way).
    monkeypatch.setenv("AGENTBRIDGE_WORKTREE_DIR", str(tmp_path.parent / "agentbridge-wt"))
    # Persist chat history to a temp dir, not the real ~/.agentbridge.
    monkeypatch.setenv("AGENTBRIDGE_STATE_DIR", str(tmp_path.parent / "agentbridge-state"))
    config.get_settings.cache_clear()
    config.github_token.cache_clear()
    monkeypatch.setattr(config.Settings, "github_token", property(lambda self: "fake-token"))
    # Register the fake adapters.
    monkeypatch.setitem(registry._ADAPTERS, "fake", FakeAdapter)
    monkeypatch.setitem(registry._ADAPTERS, "ifake", InteractiveFakeAdapter)
    # Mock the GitHub PR API and the push (no real remote).
    monkeypatch.setattr(git_service.GitService, "push", lambda self, *a, **k: None)

    class FakeResp:
        status_code = 201

        def json(self):
            return {"html_url": "https://github.com/acme/widgets/pull/7", "number": 7}

    pr_payloads: list[dict] = []

    def _fake_post(*a, **k):
        pr_payloads.append(k.get("json") or {})
        return FakeResp()

    monkeypatch.setattr(git_service.httpx, "post", _fake_post)

    from agentbridge.main import app

    return TestClient(app), tmp_path, pr_payloads


def _recv_until(ws, type_, limit=20):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == type_:
            return msg
    raise AssertionError(f"did not receive '{type_}'")


def test_full_session_flow(client):
    tc, repo, _ = client
    with tc.websocket_connect("/ws") as ws:
        ws.send_json({"type": "list_agents"})
        agents = {a["name"] for a in _recv_until(ws, "agents")["agents"]}
        assert "fake" in agents

        ws.send_json({"type": "start_session", "agent": "fake", "title": "add feature"})
        started = _recv_until(ws, "session_started")
        assert started["branch"] == "main"
        chat_id = started["chat_id"]

        ws.send_json({"type": "user_message", "chat_id": chat_id, "text": "add a feature"})
        _recv_until(ws, "file_changes")   # turn complete
        # File the agent created is really on disk — it edits in place while you work.
        assert (repo / "feature.txt").exists()

        # A pre-existing, unrelated change the user is also working on — the agent must NOT
        # sweep this into its PR.
        (repo / "my_notes.txt").write_text("personal wip\n")

        # Open a PR: only the agent's file is committed onto a derived branch worktree; the
        # user's unrelated change is left in the workspace.
        ws.send_json({"type": "create_pr", "chat_id": chat_id, "title": "Add feature"})
        created = _recv_until(ws, "branch_created")
        assert created["branch"].startswith("agentbridge/")
        assert created["worktree_path"]               # committed onto a real worktree
        pr = _recv_until(ws, "pr_created")
        assert pr["url"].endswith("/pull/7")
        assert pr["number"] == 7
        assert not (repo / "feature.txt").exists()    # the agent's file moved onto the branch
        assert (repo / "my_notes.txt").exists()       # the unrelated change stays put
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "my_notes.txt" in porcelain and "feature.txt" not in porcelain


def test_second_pr_does_not_commit_manual_changes(client):
    """The reported bug: after a PR, a purely manual edit (no agent turn) must NOT be swept
    into a commit. With nothing of the agent's left, Create PR reports that and touches nothing."""
    tc, repo, _ = client
    with tc.websocket_connect("/ws") as ws:
        ws.send_json({"type": "start_session", "agent": "fake", "title": "feat"})
        chat_id = _recv_until(ws, "session_started")["chat_id"]
        ws.send_json({"type": "user_message", "chat_id": chat_id, "text": "add a feature"})
        _recv_until(ws, "file_changes")
        ws.send_json({"type": "create_pr", "chat_id": chat_id, "title": "Add feature"})
        _recv_until(ws, "pr_created")

        # The user now edits a file by hand — no agent involvement.
        (repo / "manual.txt").write_text("hand-edited\n")
        ws.send_json({"type": "create_pr", "chat_id": chat_id})
        err = _recv_until(ws, "error")
        assert "No new agent changes" in err["message"]
        # The manual change is untouched — still dirty in the workspace, never committed.
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "manual.txt" in porcelain


def test_pr_title_auto_derived_from_agent_summary(client):
    """With no title typed, the backend names the PR from the agent's own reply."""
    tc, repo, pr_payloads = client
    with tc.websocket_connect("/ws") as ws:
        ws.send_json({"type": "start_session", "agent": "fake", "title": "feat"})
        chat_id = _recv_until(ws, "session_started")["chat_id"]
        ws.send_json({"type": "user_message", "chat_id": chat_id, "text": "add a feature"})
        _recv_until(ws, "file_changes")
        # No title field at all -> derived from the agent's "Added feature.txt" reply.
        ws.send_json({"type": "create_pr", "chat_id": chat_id})
        _recv_until(ws, "pr_created")
    assert pr_payloads, "PR API was not called"
    payload = pr_payloads[-1]
    assert payload["title"] == "Added feature.txt"
    assert "feature.txt" in payload["body"]  # body lists the changed file


def test_interactive_prompt_round_trip(client):
    """The turn blocks on a prompt; the server must still receive agent_response and
    route it so the turn can complete. This validates concurrent message handling."""
    tc, repo, _ = client
    with tc.websocket_connect("/ws") as ws:
        ws.send_json({"type": "start_session", "agent": "ifake"})
        started = _recv_until(ws, "session_started")
        chat_id = started["chat_id"]

        ws.send_json({"type": "user_message", "chat_id": chat_id, "text": "edit app.py"})
        prompt = _recv_until(ws, "agent_prompt")
        assert prompt["options"] == ["Allow", "Deny"]

        # Reply while the agent turn is still blocked awaiting this answer.
        ws.send_json({"type": "agent_response", "chat_id": chat_id,
                      "request_id": prompt["request_id"], "answer": "Allow"})

        # The turn resumes, the file is written, and we return to idle.
        _recv_until(ws, "file_changes")
        assert (repo / "app.py").exists()


def test_chat_persistence_and_resume(client):
    """Chats persist across connections: list, reopen with replayed history + resume id."""
    tc, repo, _ = client
    with tc.websocket_connect("/ws") as ws:
        ws.send_json({"type": "start_session", "agent": "fake", "title": "first"})
        chat_id = _recv_until(ws, "session_started")["chat_id"]
        ws.send_json({"type": "user_message", "chat_id": chat_id, "text": "do a thing"})
        _recv_until(ws, "status")  # let the turn run to idle
        _drain_idle(ws, chat_id)

    # New connection (simulates a page refresh): the chat is still listed.
    with tc.websocket_connect("/ws") as ws:
        ws.send_json({"type": "list_chats"})
        chats = _recv_until(ws, "chats")["chats"]
        assert any(c["id"] == chat_id and c["title"] == "first" for c in chats)

        # Reopen it: the persisted transcript is replayed.
        ws.send_json({"type": "open_chat", "chat_id": chat_id})
        _recv_until(ws, "session_started")
        history = _recv_until(ws, "chat_history")
        kinds = [e["kind"] for e in history["entries"]]
        assert "user" in kinds and "agent" in kinds

        # Delete it.
        ws.send_json({"type": "delete_chat", "chat_id": chat_id})
        deleted = _recv_until(ws, "chat_deleted")
        assert deleted["chat_id"] == chat_id
        remaining = {c["id"] for c in _recv_until(ws, "chats")["chats"]}
        assert chat_id not in remaining


def _drain_idle(ws, chat_id, limit=30):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == "status" and msg.get("state") == "idle":
            return
    raise AssertionError("turn did not reach idle")
