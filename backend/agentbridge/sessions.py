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
import base64
import binascii
import os
import re
from pathlib import Path
from typing import Awaitable, Callable

from . import protocol as P
from .agents.base import AgentEvent, SessionContext
from .agents.registry import create_adapter, get_adapter_class, list_agent_info
from .git_service import GitError, GitService, PullRequest
from .mcp_config import McpServer, McpStore
from .store import ChatRecord, ChatStore

Send = Callable[[P.ServerMessage], Awaitable[None]]


def _max_upload_bytes() -> int:
    """Per-file upload cap, in bytes. Override with AGENTBRIDGE_MAX_UPLOAD_MB (default 25)."""
    try:
        mb = float(os.environ.get("AGENTBRIDGE_MAX_UPLOAD_MB", "25"))
    except ValueError:
        mb = 25.0
    return int(mb * 1024 * 1024)


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def _safe_filename(name: str) -> str:
    """Reduce an arbitrary client-supplied name to a safe basename (no path traversal)."""
    base = Path(str(name).replace("\\", "/")).name.strip().lstrip(".")
    base = re.sub(r"[^A-Za-z0-9._ ()+-]", "_", base)
    return base or "file"


def _dedupe_path(path: Path) -> Path:
    """If ``path`` exists, append ' (1)', ' (2)', … before the extension until it's free."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem} ({uuid4_hex()}){suffix}")


def uuid4_hex() -> str:
    import uuid

    return uuid.uuid4().hex[:8]


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
        mcp_for_sdk: Callable[[], dict],
    ) -> None:
        self.record = record
        self.chat_id = record.id
        self.workspace = workspace
        self.send = send
        self.github_token = github_token
        self.store = store
        self.turn_lock = turn_lock
        self.notify_chats = notify_chats
        # Reads the workspace's enabled MCP servers ({name: sdk_config}) at adapter-start time.
        self.mcp_for_sdk = mcp_for_sdk

        self.git = GitService(workspace)
        self.adapter = None
        self._adapter_lock = asyncio.Lock()  # guards lazy adapter creation (warmup vs. first turn)
        if record.base_branch is None:
            record.base_branch = self.git.current_branch()
        self._turn_active = False
        self._turn_text: list[str] = []  # accumulates stdout for the current turn

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    async def handle(self, msg: P.ClientMessage) -> None:
        handler = {
            "user_message": self._on_user_message,
            "upload_file": self._on_upload_file,
            "agent_response": self._on_agent_response,
            "stop": self._on_stop,
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

    async def warmup(self) -> None:
        """Eagerly start the agent (spawn the CLI / open the SDK client + load workspace
        settings/CLAUDE.md) when a chat is opened, so the first message isn't slowed by cold
        start. Best-effort — any real failure surfaces on the first actual turn."""
        try:
            await self._ensure_adapter()
        except Exception:  # noqa: BLE001
            pass

    async def _ensure_adapter(self) -> None:
        if self.adapter is not None:
            return
        async with self._adapter_lock:
            if self.adapter is not None:  # another caller (e.g. warmup) won the race
                return
            try:
                adapter = create_adapter(self.record.agent, self.workspace)
            except KeyError as exc:
                raise RuntimeError(str(exc)) from exc
            if not adapter.is_available():
                raise RuntimeError(f"Agent '{self.record.agent}' is not available on this machine.")

            ctx = SessionContext(
                session_id=self.chat_id, title=self.record.title, resume=self.record.resume_id,
                mcp_servers=self.mcp_for_sdk(),
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
                    SessionContext(
                        session_id=self.chat_id, title=self.record.title, resume=None,
                        mcp_servers=self.mcp_for_sdk(),
                    )
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

        # Orient the agent on the very first turn so it doesn't hunt around for paths.
        first_turn = not any(e.get("kind") == "agent" for e in self.record.transcript)

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

        # Honor the widget's auto-approve toggle and selected mode for this turn (no-ops for
        # adapters that don't support them).
        self.adapter.set_auto_approve(msg.auto_approve)  # type: ignore[union-attr]
        self.adapter.set_mode(msg.mode)  # type: ignore[union-attr]
        self.adapter.set_model(msg.model)  # type: ignore[union-attr]
        self.adapter.set_effort(msg.effort)  # type: ignore[union-attr]

        text = msg.text
        sections = []
        if first_turn:
            sections.append(self._workspace_map())
        sections.append(self._format_context(msg.context))
        sections.append(self._format_attachments(msg.attachments))
        preamble = "\n\n".join(s for s in sections if s)
        if preamble:
            text = f"{preamble}\n\n---\n\n{text}"

        self._turn_text = []
        await self.send(P.Status(chat_id=self.chat_id, state="working"))

        # Snapshot the dirty set before the turn. Anything dirty *now* is the user's pre-existing
        # work and must never be attributed to the agent; anything that becomes dirty during the
        # turn is the agent's doing (this also catches edits made via Bash/sed that don't surface
        # as edit-tool events).
        before = self._dirty_paths()

        # Mark active only once we're committed to running the turn, and always clear it in the
        # finally — otherwise an exception before/around the loop would wedge the chat as
        # perpetually "working" and block Stop. Serialize across chats: only one agent turn
        # touches the workspace at a time.
        self._turn_active = True
        async with self.turn_lock:
            try:
                async for event in self.adapter.send(text):  # type: ignore[union-attr]
                    await self._emit(event)
            except Exception as exc:  # noqa: BLE001
                await self.send(P.ErrorMessage(message=f"Agent run failed: {exc}", chat_id=self.chat_id))
            finally:
                self._turn_active = False

        for path in self._dirty_paths() - before:
            if path not in self.record.touched:
                self.record.touched.append(path)

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
        await self.send(P.Status(chat_id=self.chat_id, state="idle"))

    async def _on_agent_response(self, msg: P.AgentResponse) -> None:
        if self.adapter is not None:
            await self.adapter.resolve_prompt(msg.request_id, msg.answer)

    async def _on_upload_file(self, msg: P.UploadFile) -> None:
        """Store an attached file under the workspace's gitignored .agentbridge/uploads/ and
        return the path the agent can read. Writing is blocking I/O, so it runs off the loop."""
        async def fail(reason: str) -> None:
            await self.send(P.FileUploaded(chat_id=self.chat_id, upload_id=msg.upload_id, ok=False, error=reason))

        try:
            raw = base64.b64decode(msg.data, validate=True)
        except (binascii.Error, ValueError):
            await fail("file data was not valid base64")
            return
        limit = _max_upload_bytes()
        if len(raw) > limit:
            await fail(f"{_human_size(len(raw))} exceeds the {_human_size(limit)} limit")
            return
        try:
            rel = await asyncio.to_thread(self._write_upload, msg.name, raw)
        except OSError as exc:
            await fail(str(exc))
            return
        await self.send(P.FileUploaded(
            chat_id=self.chat_id, upload_id=msg.upload_id, ok=True,
            name=Path(rel).name, path=rel, size=len(raw),
        ))

    def _write_upload(self, name: str, data: bytes) -> str:
        """Write upload bytes into .agentbridge/uploads/<chat>/, de-duplicating the filename.
        Returns the workspace-relative path. Blocking; call via asyncio.to_thread."""
        uploads = self.workspace / ".agentbridge" / "uploads" / self.chat_id
        uploads.mkdir(parents=True, exist_ok=True)
        self._ensure_agentbridge_gitignored()
        dest = _dedupe_path(uploads / _safe_filename(name))
        dest.write_bytes(data)
        return dest.relative_to(self.workspace).as_posix()

    def _ensure_agentbridge_gitignored(self) -> None:
        """Make the whole .agentbridge/ directory invisible to git so uploads never show as
        changes or get swept into a PR — without touching the user's own .gitignore. A
        self-ignoring .gitignore inside the dir does exactly that."""
        marker = self.workspace / ".agentbridge" / ".gitignore"
        if not marker.exists():
            marker.write_text("# Created by AgentBridge — keeps uploads/scratch out of git.\n*\n")

    async def _on_stop(self, msg: P.StopAgent) -> None:
        """Cancel the in-flight turn. The running turn loop then ends and emits idle status."""
        if not self._turn_active or self.adapter is None:
            return
        stopped = await self.adapter.interrupt()
        if not stopped:
            await self.send(
                P.ErrorMessage(message="This agent can't be stopped mid-run.", chat_id=self.chat_id)
            )

    async def _on_create_pr(self, msg: P.CreatePR) -> None:
        """Commit ONLY the files the agent touched onto a fresh branch worktree, push, and
        open a PR. The user's other (pre-existing) workspace changes are left alone."""
        # Files the agent edited that are still actually changed on disk.
        touched = [p for p in self.record.touched if self.git.is_path_dirty(p)]
        if not touched:
            # Never fall back to committing *everything* — that would sweep up the user's own
            # manual/unrelated changes. With nothing of the agent's left to commit, stop here.
            text = (
                "No new agent changes since the last PR."
                if self.record.target_branch
                else "No agent changes to commit yet."
            )
            await self.send(P.ErrorMessage(message=text, chat_id=self.chat_id))
            return

        # Have the model write the title/description (isolated one-shot), with a deterministic
        # fallback. Only when the user didn't type both fields already.
        gen_title = gen_summary = None
        if not ((msg.title or "").strip() and (msg.body or "").strip()):
            gen_title, gen_summary = await self._model_pr(touched)
        title, body = self._pr_meta(msg.title, msg.body, touched, gen_title=gen_title, gen_summary=gen_summary)
        branch = self.record.target_branch or self.git.suggest_branch_name(title)
        try:
            # The whole sequence is blocking work — git subprocesses plus a synchronous GitHub
            # HTTP call — so run it off the event loop; otherwise it would stall every other
            # WebSocket connection (and turn) for the duration of the push/PR round-trip.
            path, pr = await asyncio.to_thread(self._open_pr_blocking, branch, title, body, touched)
        except Exception as exc:  # noqa: BLE001 — surface any failure, but never lose the user's work
            # The workspace was never modified (changes are only reset after the PR is live), so the
            # agent's edits are still safe — make that explicit and let the user just retry.
            reason = str(exc) or exc.__class__.__name__
            await self.send(P.ErrorMessage(
                message=(f"Couldn't create the PR: {reason}\n"
                         "Your changes are safe in the workspace — fix the issue and click Create PR again."),
                chat_id=self.chat_id,
            ))
            return

        self.record.touched = []  # committed — start fresh for any further edits in this chat
        self.record.target_branch = branch
        self.record.transcript.append({"kind": "branch", "branch": branch, "worktree_path": str(path)})
        self.record.transcript.append({"kind": "pr", "url": pr.url, "number": pr.number})
        await self._refresh_files()
        self.store.save(self.record)
        await self.notify_chats()
        await self.send(P.BranchCreated(chat_id=self.chat_id, branch=branch, worktree_path=str(path)))
        await self.send(P.PRCreated(chat_id=self.chat_id, url=pr.url, number=pr.number))
        await self._send_file_changes()

    def _open_pr_blocking(
        self, branch: str, title: str, body: str, touched: list[str]
    ) -> tuple[Path, PullRequest]:
        """Create the PR transactionally so a failure never loses the user's work.

        The agent's files are *copied* onto the branch worktree (the workspace is left untouched),
        then committed, pushed, and turned into a PR. Only once the PR is live do we reset those
        files in the workspace. If anything fails, the half-built worktree/branch is torn down and
        the workspace still has every change — the user can simply retry. Pure blocking work, so it
        runs in a worker thread via ``asyncio.to_thread``.
        """
        path = self.git.ensure_worktree(branch)
        try:
            self.git.copy_paths_to(path, touched)
            wt_git = GitService(path)
            if wt_git.commit_all(title) is None:
                raise GitError("Nothing to commit for the agent's changes.")
            wt_git.push(branch, token=self.github_token)
            pr = wt_git.create_pull_request(title=title, head=branch, body=body, token=self.github_token)
        except Exception:
            # Workspace was never touched; just remove the half-built branch so a retry is clean.
            self.git.discard_worktree(path, branch)
            raise
        # PR is live — now (and only now) reset the workspace for those files, since they're on the
        # branch. A failure here is harmless (the PR already exists); keep it out of the try above.
        self.git.discard_paths(touched)
        return path, pr

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _emit(self, event: AgentEvent) -> None:
        if event.kind == "chunk":
            if event.stream == "stdout":
                self._turn_text.append(event.text)
            await self.send(P.AgentChunk(chat_id=self.chat_id, text=event.text, stream=event.stream))
        elif event.kind == "file_touched" and event.path:
            # Remember what the agent itself changed (workspace-relative) so a PR commits only
            # those files. Claude reports absolute paths; normalize for reliable git pathspecs.
            rel = self._rel_path(event.path)
            if rel and rel not in self.record.touched:
                self.record.touched.append(rel)
        elif event.kind == "prompt" and event.request_id:
            await self.send(
                P.AgentPrompt(chat_id=self.chat_id, request_id=event.request_id, prompt=event.text, options=event.options)
            )
        elif event.kind == "error":
            await self.send(P.ErrorMessage(message=event.text, chat_id=self.chat_id))

    def _format_attachments(self, attachments: list[str]) -> str:
        """Point the agent at files the user uploaded for this turn. Only list ones that actually
        landed under the uploads dir (defends against spoofed/relative paths from the client)."""
        if not attachments:
            return ""
        prefix = ".agentbridge/uploads/"
        valid = [p for p in attachments if p.startswith(prefix) and (self.workspace / p).is_file()]
        if not valid:
            return ""
        listing = "\n".join(f"- {p}" for p in valid)
        return (
            "[Attached files] The user attached these files to this message (paths are relative to "
            "the workspace root); read them as needed:\n" + listing
        )

    def _workspace_map(self) -> str:
        """A one-time orientation map of the repo so the agent reads the right paths instead of
        guessing. Paths are relative to the workspace root (the agent's working directory)."""
        try:
            entries = self.git.top_level_entries()
        except GitError:
            entries = []
        if not entries:
            return ""
        return (
            "[Workspace] You are working in this repository's root directory; all paths are "
            "relative to it. Top-level entries:\n" + "  ".join(entries)
        )

    def _rel_path(self, path: str) -> str | None:
        """Normalize an agent-reported path to a workspace-relative one (git pathspec)."""
        p = Path(path)
        if not p.is_absolute():
            return path
        try:
            return str(p.relative_to(self.workspace))
        except ValueError:
            return None  # edited outside the workspace — not part of this repo's PR

    def _dirty_paths(self) -> set[str]:
        """The set of workspace-relative paths with uncommitted changes right now."""
        try:
            return {c.path for c in self.git.status()}
        except GitError:
            return set()

    # ------------------------------------------------------------------ #
    # PR title / body
    # ------------------------------------------------------------------ #

    # Lead-in / filler the agent tends to open its summary with — useless in a PR title or body.
    _SUMMARY_PREAMBLE_RE = re.compile(
        r"^(done|sure|ok(ay)?|great|perfect|all set|finished|complete|got it)\b[.! ]*$"
        r"|here'?s (a |the )?(summary|what|breakdown|rundown|overview)"
        r"|here is (a |the )?(summary|what|breakdown|rundown|overview)"
        r"|summary of (the )?(changes|what)"
        r"|what (i )?(changed|did)"
        r"|i'?ve (made|implemented|added|done|completed)\b.*\b(changes|following|below)\b",
        re.I,
    )

    def _pr_meta(
        self,
        user_title: str | None,
        user_body: str | None,
        touched: list[str],
        gen_title: str | None = None,
        gen_summary: str | None = None,
    ) -> tuple[str, str]:
        """Resolve the PR title and body. Precedence: what the user typed, then what the model
        wrote (``gen_*``), then a deterministic fallback (title from the user's request; body from
        the agent's summary with its filler preamble stripped). The changed-file list and footer
        are always appended to a derived body."""
        summary = self._last_agent_text()
        title = (user_title or "").strip()
        if not title:
            title = (
                (gen_title or "").strip()
                or self._title_from_request(self._first_user_text())
                or self._title_from_summary(summary)
                or (self.record.title or "").strip()
                or "AgentBridge changes"
            )
        title = re.sub(r"\s+", " ", title).strip().strip('"').rstrip(".:")[:72] or "AgentBridge changes"
        body = (user_body or "").strip() or self._build_pr_body((gen_summary or summary), touched)
        return title, body

    async def _model_pr(self, touched: list[str]) -> tuple[str | None, str | None]:
        """Ask the agent to write a PR title + description, isolated from the chat session. Returns
        (title, summary), or (None, None) if unavailable/failed — caller falls back to heuristics."""
        adapter = self.adapter
        if adapter is None:
            try:
                adapter = create_adapter(self.record.agent, self.workspace)
            except Exception:  # noqa: BLE001
                return None, None
        try:
            text = await adapter.summarize_pr(self._pr_prompt(touched))
        except Exception:  # noqa: BLE001 — never let summary generation break PR creation
            return None, None
        if not text:
            return None, None
        return self._parse_model_pr(text)

    def _pr_prompt(self, touched: list[str]) -> str:
        request = self._first_user_text() or (self.record.title or "")
        summary = self._last_agent_text()
        files = "\n".join(f"- {p}" for p in sorted(touched)) or "(none reported)"
        return (
            "Write a GitHub pull request title and description for this change. Be concise and "
            "specific; describe what changed and why, not the conversation.\n\n"
            f"User's request:\n{request}\n\n"
            f"What the coding agent reported doing:\n{summary}\n\n"
            f"Files changed:\n{files}\n\n"
            "Respond with ONLY:\n"
            "- Line 1: a concise, imperative PR title (max 72 chars, no trailing period).\n"
            "- Then a blank line.\n"
            "- Then the PR description in GitHub markdown: a one or two sentence overview, then a "
            "short bullet list of the key changes.\n"
            "Do not include a 'Files changed' section, code fences around the whole answer, or any "
            "preamble like 'Here's the PR'. Do not use tools or ask questions."
        )

    @staticmethod
    def _parse_model_pr(text: str) -> tuple[str | None, str | None]:
        """Split the model's reply into (title, body): first non-empty line is the title."""
        lines = text.strip().splitlines()
        title, idx = "", 0
        for i, line in enumerate(lines):
            if line.strip():
                title = re.sub(r"[`*_#>]+", "", line).strip()
                title = re.sub(r"^(pr )?title\s*[:\-]\s*", "", title, flags=re.I)
                title = title.strip().strip('"').rstrip(".:")
                idx = i + 1
                break
        summary = "\n".join(lines[idx:]).strip()
        summary = re.sub(r"^(description|body|summary)\s*[:\-]\s*", "", summary, flags=re.I).strip()
        return (title[:72] or None), (summary or None)

    def _first_user_text(self) -> str:
        for entry in self.record.transcript:
            if entry.get("kind") == "user" and entry.get("text"):
                return str(entry["text"]).strip()
        return ""

    def _last_agent_text(self) -> str:
        for entry in reversed(self.record.transcript):
            if entry.get("kind") == "agent" and entry.get("text"):
                return str(entry["text"]).strip()
        return ""

    @staticmethod
    def _title_from_request(text: str) -> str:
        """Turn the user's request into a concise title: first sentence, capitalized, trimmed."""
        s = re.sub(r"\s+", " ", (text or "").strip())
        if len(s) < 4:
            return ""
        s = re.split(r"(?<=[.!?])\s", s, 1)[0].strip().rstrip(".")
        if s:
            s = s[0].upper() + s[1:]
        return s[:72]

    @staticmethod
    def _title_from_summary(summary: str) -> str:
        """First substantive sentence of the agent's reply — skipping filler and section headers."""
        for line in summary.splitlines():
            s = re.sub(r"[`*_#>]+", "", line).strip().lstrip("-•* ").strip()
            s = re.sub(r"\s+", " ", s)
            if len(s) < 8 or s.endswith(":"):          # too short, or a section header / lead-in
                continue
            if Session._SUMMARY_PREAMBLE_RE.search(s):  # "Done.", "Here's a summary…", etc.
                continue
            return re.split(r"(?<=[.!?])\s", s, 1)[0].rstrip(".")[:72]
        return ""

    @classmethod
    def _strip_summary_preamble(cls, summary: str) -> str:
        """Drop leading blank/filler lines ("Done. Here's a summary of what changed:") so the body
        starts at the real content. Only strips from the top — never touches mid-body lines."""
        lines = summary.splitlines()
        i = 0
        while i < len(lines):
            s = re.sub(r"[`*_#>]+", "", lines[i]).strip()
            if not s or cls._SUMMARY_PREAMBLE_RE.search(s):
                i += 1
                continue
            break
        return "\n".join(lines[i:]).strip()

    @classmethod
    def _build_pr_body(cls, summary: str, touched: list[str]) -> str:
        parts: list[str] = []
        cleaned = cls._strip_summary_preamble(summary)
        if cleaned:
            parts.append("## Summary\n\n" + cleaned)
        if touched:
            files = "\n".join(f"- `{p}`" for p in sorted(touched))
            parts.append(f"## Files changed\n\n{files}")
        parts.append("_Opened via AgentBridge._")
        return "\n\n".join(parts)

    def _agent_file_changes(self) -> list[P.FileChange]:
        """Working-tree changes limited to files THIS agent touched in this chat, so the chat's
        file list never shows the user's own / pre-existing changes."""
        try:
            changes = self.git.status()
        except GitError:
            return []
        touched = set(self.record.touched)
        return [c for c in changes if c.path in touched]

    async def _refresh_files(self) -> None:
        self.record.files = [c.model_dump() for c in self._agent_file_changes()]

    async def _send_file_changes(self) -> None:
        await self.send(P.FileChanges(chat_id=self.chat_id, files=self._agent_file_changes()))

    def _format_context(self, ctx: dict | None) -> str:
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
                # For Next.js, the route maps straight to a page file — the most reliable pointer,
                # and it also tells the agent which app in a monorepo this route belongs to.
                route_file = self.git.resolve_route_path(str(page["route"]))
                if route_file:
                    lines.append(f"- Route file (the page for this route): {route_file}")
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

            # Resolve the source file. Prefer the browser's source hint (it carries a line
            # number); when that's absent — e.g. React 19, which dropped _debugSource — locate
            # the file by component name. The nearest fiber component is often a library internal
            # (e.g. Ant Design's "Wave"), so walk the component chain innermost-first and pick the
            # first name that maps to a file in THIS repo — that's the user's component.
            src = el.get("source") if isinstance(el.get("source"), dict) else {}
            raw_file = src.get("file")
            line_suffix = f":{src.get('line')}" if src.get("line") else ""
            resolved = self.git.resolve_tracked_path(str(raw_file)) if raw_file else None

            chain = el.get("componentChain") if isinstance(el.get("componentChain"), list) else []
            comp_file = comp_name = None
            if not resolved:
                found = self.git.resolve_component_in([el.get("component"), *chain])
                if found:
                    comp_name, comp_file = found

            # Prefer the user component we actually located; else fall back to the nearest name.
            owning = comp_name or el.get("component")
            if owning:
                lines.append(f"- Owning component: {owning}")
            if el.get("selector"):
                lines.append(f"- CSS selector: {el['selector']}")
            if el.get("text"):
                lines.append(f"- Text content: {el['text']}")
            if resolved:
                lines.append(f"- Source file (open this first): {resolved}{line_suffix}")
            elif comp_file:
                lines.append(f"- Source file (open this first): {comp_file}")
            elif raw_file:
                lines.append(f"- Source hint (from the browser; may need locating): {raw_file}{line_suffix}")
        return "\n".join(lines).strip()


class ChatHub:
    """Per-connection manager: owns the store, the live sessions, and the shared turn lock."""

    def __init__(self, workspace: Path, send: Send, github_token: str | None) -> None:
        self.workspace = workspace
        self.send = send
        self.github_token = github_token
        self.store = ChatStore(workspace)
        self.mcp = McpStore(workspace)
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
        elif msg.type == "list_mcp":
            await self._send_mcp()
        elif msg.type == "save_mcp":
            await self._save_mcp(msg)
        elif msg.type == "delete_mcp":
            await self._delete_mcp(msg)
        elif msg.type == "toggle_mcp":
            await self._toggle_mcp(msg)
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
            mcp_for_sdk=self.mcp.to_sdk,
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
        session = self._make_session(record)
        self.sessions[record.id] = session
        await self.send(
            P.SessionStarted(chat_id=record.id, agent=record.agent, title=record.title, branch=record.base_branch)
        )
        await self._send_chats()
        asyncio.create_task(session.warmup())  # pre-start the agent so the first turn is snappy

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
        asyncio.create_task(session.warmup())  # warm the (possibly resumed) agent in the background

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

    # ------------------------------------------------------------------ #
    # MCP servers (plugins)
    # ------------------------------------------------------------------ #

    async def _send_mcp(self) -> None:
        servers = [P.McpServerSpec(**vars(s)) for s in self.mcp.list()]
        await self.send(P.McpServers(servers=servers))

    async def _save_mcp(self, msg: P.SaveMcp) -> None:
        server = McpServer(**msg.server.model_dump())
        if not server.is_valid():
            need = "a command" if server.transport == "stdio" else "a url"
            await self.send(P.ErrorMessage(message=f"Can't save plugin '{server.name}': it needs {need}."))
            return
        self.mcp.save(server)
        await self._send_mcp()
        await self._restart_idle_adapters()

    async def _delete_mcp(self, msg: P.DeleteMcp) -> None:
        self.mcp.delete(msg.name)
        await self._send_mcp()
        await self._restart_idle_adapters()

    async def _toggle_mcp(self, msg: P.ToggleMcp) -> None:
        self.mcp.set_enabled(msg.name, msg.enabled)
        await self._send_mcp()
        await self._restart_idle_adapters()

    async def _restart_idle_adapters(self) -> None:
        """MCP config is read when an adapter starts, so drop any idle (not mid-turn) adapters;
        the next turn re-creates them with the new plugin set (resuming via the persisted id).
        Adapters busy in a turn are left alone and pick up the change on their next turn."""
        for session in list(self.sessions.values()):
            if session.adapter is not None and not session._turn_active:
                await session.close()
