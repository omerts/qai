"""Session orchestration: bind a WebSocket to an agent + the git workspace.

One :class:`Session` per connected widget. It owns the active :class:`AgentAdapter`,
translates :class:`AgentEvent`s into protocol messages, and applies the user-controlled
branch / PR actions. Branching is **never automatic** — the session only *suggests*
branching (``branch_suggested``); the user triggers ``create_branch``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Awaitable, Callable

from . import protocol as P
from .agents.base import AgentAdapter, AgentEvent, SessionContext
from .agents.registry import create_adapter, list_agent_info
from .git_service import GitError, GitService

Send = Callable[[P.ServerMessage], Awaitable[None]]


class Session:
    """A single widget connection's worth of state."""

    def __init__(self, workspace: Path, send: Send, github_token: str | None) -> None:
        self.workspace = workspace
        self.send = send
        self.github_token = github_token

        self.id = uuid.uuid4().hex[:12]
        self.adapter: AgentAdapter | None = None
        self.title: str | None = None
        self.git = GitService(workspace)
        # The branch we were on when the session started — used to decide whether to
        # suggest branching before the first substantive edit.
        self.base_branch = self.git.current_branch()
        self._branch_created = False
        self._suggested_branch = False
        self._turn_active = False  # guards against overlapping agent turns

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    async def handle(self, msg: P.ClientMessage) -> None:
        handler = {
            "list_agents": self._on_list_agents,
            "start_session": self._on_start_session,
            "user_message": self._on_user_message,
            "agent_response": self._on_agent_response,
            "create_branch": self._on_create_branch,
            "create_pr": self._on_create_pr,
            "end_session": self._on_end_session,
        }.get(msg.type)
        if handler is None:
            return
        await handler(msg)  # type: ignore[arg-type]

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.stop()
            self.adapter = None

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #

    async def _on_list_agents(self, _msg: P.ListAgents) -> None:
        await self.send(P.Agents(agents=list_agent_info()))

    async def _on_start_session(self, msg: P.StartSession) -> None:
        if self.adapter is not None:
            await self.adapter.stop()

        try:
            adapter = create_adapter(msg.agent, self.workspace)
        except KeyError as exc:
            await self.send(P.ErrorMessage(message=str(exc)))
            return

        if not adapter.is_available():
            await self.send(
                P.ErrorMessage(message=f"Agent '{msg.agent}' is not available on this machine.")
            )
            return

        self.title = msg.title
        try:
            await adapter.start(SessionContext(session_id=self.id, title=msg.title))
        except Exception as exc:  # noqa: BLE001
            await self.send(P.ErrorMessage(message=f"Could not start agent: {exc}"))
            return

        self.adapter = adapter
        self.base_branch = self.git.current_branch()
        await self.send(
            P.SessionStarted(session_id=self.id, agent=adapter.name, branch=self.base_branch)
        )

    async def _on_user_message(self, msg: P.UserMessage) -> None:
        if self.adapter is None:
            await self.send(P.ErrorMessage(message="No active session. Pick an agent first."))
            return
        if self._turn_active:
            await self.send(P.ErrorMessage(message="The agent is still working on the previous message."))
            return

        self._turn_active = True
        await self.send(P.Status(state="working"))
        touched_a_file = False

        try:
            async for event in self.adapter.send(msg.text):
                if event.kind == "file_touched":
                    touched_a_file = True
                await self._emit(event)
                # Offer to branch the first time the agent touches a file while we are
                # still sitting on the base branch. Advisory only.
                if event.kind == "file_touched":
                    await self._maybe_suggest_branch()
        except Exception as exc:  # noqa: BLE001
            await self.send(P.ErrorMessage(message=f"Agent run failed: {exc}"))
        finally:
            self._turn_active = False

        # Report the resulting working-tree changes.
        await self._send_file_changes()
        # Fallback suggestion: even if the adapter didn't emit file_touched events,
        # suggest branching when the working tree is now dirty on the base branch.
        if not touched_a_file and self.git.has_uncommitted_changes():
            await self._maybe_suggest_branch()
        await self.send(P.Status(state="idle"))

    async def _on_agent_response(self, msg: P.AgentResponse) -> None:
        """Route the user's reply to an interactive prompt back into the active adapter."""
        if self.adapter is not None:
            await self.adapter.resolve_prompt(msg.request_id, msg.answer)

    async def _on_create_branch(self, msg: P.CreateBranch) -> None:
        name = msg.name or self.git.suggest_branch_name(self.title)
        try:
            branch = self.git.create_branch(name=name, base=msg.base_branch)
        except GitError as exc:
            await self.send(P.ErrorMessage(message=str(exc)))
            return
        self._branch_created = True
        await self.send(P.BranchCreated(branch=branch))

    async def _on_create_pr(self, msg: P.CreatePR) -> None:
        try:
            if self.git.has_uncommitted_changes():
                self.git.commit_all(msg.title)
            head = self.git.current_branch()
            self.git.push(head)
            pr = self.git.create_pull_request(
                title=msg.title,
                head=head,
                body=msg.body or "",
                token=self.github_token,
            )
        except GitError as exc:
            await self.send(P.ErrorMessage(message=str(exc)))
            return
        await self.send(P.PRCreated(url=pr.url, number=pr.number))

    async def _on_end_session(self, _msg: P.EndSession) -> None:
        await self.close()
        await self.send(P.Status(state="idle"))

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _emit(self, event: AgentEvent) -> None:
        """Translate one AgentEvent into a protocol message and send it."""
        if event.kind == "chunk":
            await self.send(P.AgentChunk(text=event.text, stream=event.stream))
        elif event.kind == "prompt" and event.request_id:
            await self.send(
                P.AgentPrompt(request_id=event.request_id, prompt=event.text, options=event.options)
            )
        elif event.kind == "error":
            await self.send(P.ErrorMessage(message=event.text))
        # file_touched / done are handled by the caller (file_changes summary / status).

    async def _send_file_changes(self) -> None:
        try:
            changes = self.git.status()
        except GitError:
            return
        await self.send(P.FileChanges(files=changes))

    async def _maybe_suggest_branch(self) -> None:
        """Suggest (once) branching off the base branch — purely advisory."""
        if self._suggested_branch or self._branch_created:
            return
        if self.git.current_branch() != self.base_branch:
            return
        self._suggested_branch = True
        await self.send(
            P.BranchSuggested(
                suggested_name=self.git.suggest_branch_name(self.title),
                reason=(
                    f"The agent is editing files on '{self.base_branch}'. "
                    "Create a branch to keep this session's changes isolated?"
                ),
            )
        )
