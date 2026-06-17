"""Session orchestration for a connection that multiplexes several chats.

- :class:`Session` owns one chat: its adapter (started lazily, resuming prior context when
  reopened), the git workspace, transcript persistence, and the user-controlled branch / PR
  actions. Branching is **never automatic** — the session only *suggests* it.
- :class:`ChatHub` owns one WebSocket connection: a :class:`store.ChatStore`, the live
  :class:`Session` objects keyed by chat id, and a shared turn lock that **serializes agent
  turns across chats** (they all edit the same workspace, so only one runs at a time).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from . import protocol as P
from .agents.base import AgentEvent, SessionContext
from .agents.registry import create_adapter, get_adapter_class, list_agent_info
from .git_service import GitError, GitService
from .store import ChatRecord, ChatStore

Send = Callable[[P.ServerMessage], Awaitable[None]]


class Session:
    """A single chat's worth of state (adapter + git + persisted transcript)."""

    def __init__(
        self,
        *,
        record: ChatRecord,
        workspace: Path,
        send: Send,
        github_token: str | None,
        store: ChatStore,
        turn_lock: asyncio.Lock,
        notify_chats: Callable[[], Awaitable[None]],
    ) -> None:
        self.record = record
        self.chat_id = record.id
        self.workspace = workspace
        self.send = send
        self.github_token = github_token
        self.store = store
        self.turn_lock = turn_lock
        self.notify_chats = notify_chats

        self.git = GitService(workspace)
        self.adapter = None
        if record.base_branch is None:
            record.base_branch = self.git.current_branch()
        self._suggested_branch = False
        self._branch_chosen = record.target_branch is not None
        self._turn_active = False
        self._turn_text: list[str] = []  # accumulates stdout for the current turn

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    async def handle(self, msg: P.ClientMessage) -> None:
        handler = {
            "user_message": self._on_user_message,
            "agent_response": self._on_agent_response,
            "create_branch": self._on_create_branch,
            "create_pr": self._on_create_pr,
        }.get(msg.type)
        if handler is not None:
            await handler(msg)  # type: ignore[arg-type]

    async def close(self) -> None:
        if self.adapter is not None:
            await self.adapter.stop()
            self.adapter = None

    # ------------------------------------------------------------------ #
    # Adapter lifecycle (lazy; resumes prior context when reopening a chat)
    # ------------------------------------------------------------------ #

    async def _ensure_adapter(self) -> None:
        if self.adapter is not None:
            return
        try:
            adapter = create_adapter(self.record.agent, self.workspace)
        except KeyError as exc:
            raise RuntimeError(str(exc)) from exc
        if not adapter.is_available():
            raise RuntimeError(f"Agent '{self.record.agent}' is not available on this machine.")

        ctx = SessionContext(
            session_id=self.chat_id, title=self.record.title, resume=self.record.resume_id
        )
        try:
            await adapter.start(ctx)
        except Exception:  # noqa: BLE001 — resume may fail; fall back to a fresh session
            if not self.record.resume_id:
                raise
            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001
                pass
            adapter = create_adapter(self.record.agent, self.workspace)
            await adapter.start(
                SessionContext(session_id=self.chat_id, title=self.record.title, resume=None)
            )
            self.record.resume_id = None
            await self.send(
                P.AgentChunk(
                    chat_id=self.chat_id,
                    text="(Couldn't resume the previous agent context — continuing fresh.)\n",
                    stream="stderr",
                )
            )
        self.adapter = adapter

    # ------------------------------------------------------------------ #
    # Handlers
    # ------------------------------------------------------------------ #

    async def _on_user_message(self, msg: P.UserMessage) -> None:
        if self._turn_active:
            await self.send(
                P.ErrorMessage(message="The agent is still working on the previous message.", chat_id=self.chat_id)
            )
            return

        # Record + persist the user turn immediately so history survives a crash mid-turn.
        self.record.transcript.append({"kind": "user", "text": msg.text})
        if not self.record.title:
            self.record.title = msg.text.strip()[:48] or "New chat"
        self.store.save(self.record)
        await self.notify_chats()

        try:
            await self._ensure_adapter()
        except Exception as exc:  # noqa: BLE001
            await self.send(P.ErrorMessage(message=f"Could not start agent: {exc}", chat_id=self.chat_id))
            return

        # Honor the widget's auto-approve toggle for this turn (no-op for headless adapters).
        self.adapter.set_auto_approve(msg.auto_approve)  # type: ignore[union-attr]

        text = msg.text
        preamble = self._format_context(msg.context)
        if preamble:
            text = f"{preamble}\n\n---\n\n{text}"

        self._turn_active = True
        self._turn_text = []
        await self.send(P.Status(chat_id=self.chat_id, state="working"))
        touched_a_file = False

        # Serialize across chats: only one agent turn touches the workspace at a time.
        async with self.turn_lock:
            try:
                async for event in self.adapter.send(text):  # type: ignore[union-attr]
                    if event.kind == "file_touched":
                        touched_a_file = True
                    await self._emit(event)
                    if event.kind == "file_touched":
                        await self._maybe_suggest_branch()
            except Exception as exc:  # noqa: BLE001
                await self.send(P.ErrorMessage(message=f"Agent run failed: {exc}", chat_id=self.chat_id))
            finally:
                self._turn_active = False

        # Persist the agent's reply, the resume handle, and the changed-file summary.
        agent_text = "".join(self._turn_text).strip()
        if agent_text:
            self.record.transcript.append({"kind": "agent", "text": agent_text})
        if self.adapter is not None:
            rid = self.adapter.resume_handle()
            if rid:
                self.record.resume_id = rid
        await self._refresh_files()
        self.store.save(self.record)
        await self.notify_chats()
        await self.send(
            P.FileChanges(chat_id=self.chat_id, files=[P.FileChange(**f) for f in self.record.files])
        )

        if not touched_a_file and self.git.has_uncommitted_changes():
            await self._maybe_suggest_branch()
        await self.send(P.Status(chat_id=self.chat_id, state="idle"))

    async def _on_agent_response(self, msg: P.AgentResponse) -> None:
        if self.adapter is not None:
            await self.adapter.resolve_prompt(msg.request_id, msg.answer)

    async def _on_create_branch(self, msg: P.CreateBranch) -> None:
        """User-triggered. Choose the branch this work commits to (created at PR time)."""
        self.record.target_branch = (
            self.git.sanitize_branch_name(msg.name) if msg.name else self.git.suggest_branch_name(self.record.title)
        )
        self._branch_base = msg.base_branch
        self._branch_chosen = True
        entry = {"kind": "branch", "branch": self.record.target_branch, "worktree_path": None}
        self.record.transcript.append(entry)
        self.store.save(self.record)
        await self.notify_chats()
        await self.send(P.BranchCreated(chat_id=self.chat_id, branch=self.record.target_branch))

    async def _on_create_pr(self, msg: P.CreatePR) -> None:
        if not self.git.has_uncommitted_changes() and self.record.target_branch is None:
            await self.send(P.ErrorMessage(message="No changes to commit yet.", chat_id=self.chat_id))
            return
        branch = self.record.target_branch or self.git.suggest_branch_name(self.record.title)
        base = getattr(self, "_branch_base", None)
        try:
            path = self.git.ensure_worktree(branch, base=base)
            self.git.migrate_uncommitted_to(path)
            wt_git = GitService(path)
            wt_git.commit_all(msg.title)
            wt_git.push(branch)
            pr = wt_git.create_pull_request(
                title=msg.title, head=branch, body=msg.body or "", token=self.github_token
            )
        except GitError as exc:
            await self.send(P.ErrorMessage(message=str(exc), chat_id=self.chat_id))
            return

        self.record.target_branch = branch
        self._branch_chosen = True
        self.record.transcript.append({"kind": "branch", "branch": branch, "worktree_path": str(path)})
        self.record.transcript.append({"kind": "pr", "url": pr.url, "number": pr.number})
        await self._refresh_files()
        self.store.save(self.record)
        await self.notify_chats()
        await self.send(P.BranchCreated(chat_id=self.chat_id, branch=branch, worktree_path=str(path)))
        await self.send(P.PRCreated(chat_id=self.chat_id, url=pr.url, number=pr.number))
        await self._send_file_changes()

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _emit(self, event: AgentEvent) -> None:
        if event.kind == "chunk":
            if event.stream == "stdout":
                self._turn_text.append(event.text)
            await self.send(P.AgentChunk(chat_id=self.chat_id, text=event.text, stream=event.stream))
        elif event.kind == "prompt" and event.request_id:
            await self.send(
                P.AgentPrompt(chat_id=self.chat_id, request_id=event.request_id, prompt=event.text, options=event.options)
            )
        elif event.kind == "error":
            await self.send(P.ErrorMessage(message=event.text, chat_id=self.chat_id))

    async def _refresh_files(self) -> None:
        try:
            self.record.files = [c.model_dump() for c in self.git.status()]
        except GitError:
            pass

    async def _send_file_changes(self) -> None:
        try:
            changes = self.git.status()
        except GitError:
            return
        await self.send(P.FileChanges(chat_id=self.chat_id, files=changes))

    async def _maybe_suggest_branch(self) -> None:
        if self._suggested_branch or self._branch_chosen:
            return
        if self.git.current_branch() != self.record.base_branch:
            return
        self._suggested_branch = True
        await self.send(
            P.BranchSuggested(
                chat_id=self.chat_id,
                suggested_name=self.git.suggest_branch_name(self.record.title),
                reason=(
                    f"The agent is editing on '{self.record.base_branch}'. Choose a branch for "
                    "this work? Your edits stay live in the workspace; they're committed to the "
                    "branch when you open a PR."
                ),
            )
        )

    @staticmethod
    def _format_context(ctx: dict | None) -> str:
        if not isinstance(ctx, dict):
            return ""
        lines: list[str] = []
        page = ctx.get("page")
        if isinstance(page, dict) and page:
            lines.append("[Browser context from the running app]")
            if page.get("url"):
                lines.append(f"- URL: {page['url']}")
            if page.get("route"):
                lines.append(f"- Route: {page['route']}")
            if page.get("title"):
                lines.append(f"- Page title: {page['title']}")
            fw = page.get("framework")
            if isinstance(fw, dict) and fw.get("name"):
                version = f" {fw['version']}" if fw.get("version") else ""
                lines.append(f"- Framework: {fw['name']}{version}")
            comps = page.get("components")
            if isinstance(comps, list) and comps:
                lines.append(f"- Components detected on page: {', '.join(str(c) for c in comps[:30])}")
        el = ctx.get("element")
        if isinstance(el, dict) and el:
            if lines:
                lines.append("")
            lines.append("[Element the user selected on the page]")
            if el.get("label"):
                lines.append(f"- Element: {el['label']}")
            if el.get("component"):
                lines.append(f"- Owning component: {el['component']}")
            if el.get("selector"):
                lines.append(f"- CSS selector: {el['selector']}")
            if el.get("text"):
                lines.append(f"- Text content: {el['text']}")
            src = el.get("source")
            if isinstance(src, dict) and src.get("file"):
                loc = src["file"] + (f":{src['line']}" if src.get("line") else "")
                lines.append(f"- Source hint: {loc}")
        return "\n".join(lines).strip()


class ChatHub:
    """Per-connection manager: owns the store, the live sessions, and the shared turn lock."""

    def __init__(self, workspace: Path, send: Send, github_token: str | None) -> None:
        self.workspace = workspace
        self.send = send
        self.github_token = github_token
        self.store = ChatStore(workspace)
        self.git = GitService(workspace)  # validates the workspace is a git repo
        self.sessions: dict[str, Session] = {}
        self.turn_lock = asyncio.Lock()

    async def handle(self, msg: P.ClientMessage) -> None:
        if msg.type == "list_agents":
            await self.send(P.Agents(agents=list_agent_info()))
        elif msg.type == "list_chats":
            await self._send_chats()
        elif msg.type == "start_session":
            await self._start(msg)
        elif msg.type == "open_chat":
            await self._open(msg)
        elif msg.type == "delete_chat":
            await self._delete(msg)
        elif msg.type == "end_session":
            await self._end(msg)
        else:
            chat_id = getattr(msg, "chat_id", None)
            session = await self._get_session(chat_id) if chat_id else None
            if session is None:
                await self.send(P.ErrorMessage(message="Unknown or missing chat id.", chat_id=chat_id))
                return
            await session.handle(msg)

    async def close(self) -> None:
        for session in list(self.sessions.values()):
            await session.close()
        self.sessions.clear()

    # ------------------------------------------------------------------ #

    def _make_session(self, record: ChatRecord) -> Session:
        return Session(
            record=record,
            workspace=self.workspace,
            send=self.send,
            github_token=self.github_token,
            store=self.store,
            turn_lock=self.turn_lock,
            notify_chats=self._send_chats,
        )

    async def _get_session(self, chat_id: str | None) -> Session | None:
        if not chat_id:
            return None
        if chat_id in self.sessions:
            return self.sessions[chat_id]
        record = self.store.load(chat_id)
        if record is None:
            return None
        session = self._make_session(record)
        self.sessions[chat_id] = session
        return session

    async def _start(self, msg: P.StartSession) -> None:
        try:
            cls = get_adapter_class(msg.agent)
        except KeyError as exc:
            await self.send(P.ErrorMessage(message=str(exc)))
            return
        if not cls.is_available():
            await self.send(P.ErrorMessage(message=f"Agent '{msg.agent}' is not available on this machine."))
            return

        record = self.store.create(agent=msg.agent, title=msg.title)
        record.base_branch = self.git.current_branch()
        self.store.save(record)
        self.sessions[record.id] = self._make_session(record)
        await self.send(
            P.SessionStarted(chat_id=record.id, agent=record.agent, title=record.title, branch=record.base_branch)
        )
        await self._send_chats()

    async def _open(self, msg: P.OpenChat) -> None:
        session = await self._get_session(msg.chat_id)
        if session is None:
            await self.send(P.ErrorMessage(message="That chat no longer exists.", chat_id=msg.chat_id))
            return
        rec = session.record
        await self.send(
            P.SessionStarted(
                chat_id=rec.id,
                agent=rec.agent,
                title=rec.title,
                branch=rec.base_branch or self.git.current_branch(),
            )
        )
        await self.send(
            P.ChatHistory(
                chat_id=rec.id,
                entries=rec.transcript,
                files=[P.FileChange(**f) for f in rec.files],
                branch=rec.base_branch,
                target_branch=rec.target_branch,
            )
        )

    async def _delete(self, msg: P.DeleteChat) -> None:
        session = self.sessions.pop(msg.chat_id, None)
        if session is not None:
            await session.close()
        self.store.delete(msg.chat_id)
        await self.send(P.ChatDeleted(chat_id=msg.chat_id))
        await self._send_chats()

    async def _end(self, msg: P.EndSession) -> None:
        if msg.chat_id:
            session = self.sessions.pop(msg.chat_id, None)
            if session is not None:
                await session.close()

    async def _send_chats(self) -> None:
        await self.send(P.Chats(chats=[P.ChatMeta(**m) for m in self.store.list_meta()]))
