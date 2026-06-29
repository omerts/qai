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
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from . import protocol as P
from .agents.base import AgentEvent, SessionContext
from .agents.registry import create_adapter, get_adapter_class, list_agent_info
from .git_service import GitError, GitService, PullRequest
from .mcp_config import McpServer, McpStore
from .store import ChatRecord, ChatStore

_log = logging.getLogger("agentbridge")

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
        notify_chats: Callable[[], Awaitable[None]],
        mcp_for_sdk: Callable[[], dict],
        live_mirror: Callable[[str, list[str]], Awaitable[None]],
    ) -> None:
        self.record = record
        self.chat_id = record.id
        self.workspace = workspace
        self.send = send
        self.github_token = github_token
        self.store = store
        self.notify_chats = notify_chats
        # Reads the workspace's enabled MCP servers ({name: sdk_config}) at adapter-start time.
        self.mcp_for_sdk = mcp_for_sdk
        # Mirror this chat's changed files into the workspace if it's the "live" one (for hot reload).
        self.live_mirror = live_mirror

        # repo_git points at the real repo; the agent works in this chat's private worktree (self.git,
        # set once the worktree is created) so chats never collide and can run in parallel.
        self.repo_git = GitService(workspace)
        self.git = self.repo_git
        self.worktree_path: Path | None = None
        self._worktree_ready = False
        if record.base_branch is None:
            record.base_branch = self.repo_git.current_branch()

        self.adapter = None
        self._adapter_lock = asyncio.Lock()  # guards lazy adapter creation (warmup vs. first turn)
        self._wt_lock = asyncio.Lock()       # serializes worktree creation (go-live vs. first turn)
        self._turn_lock = asyncio.Lock()     # serializes turns *within this chat* (chats run in parallel)
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
            "update_branch": self._on_update_branch,
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

    def _chat_branch(self) -> str:
        """A stable, readable branch name for this chat's worktree."""
        title = (self.record.title or "").strip()
        slug = self.repo_git.sanitize_branch_name(title) if title else ""
        slug = slug.replace("agentbridge/", "").strip("-/") or "chat"
        return f"agentbridge/{slug}-{self.chat_id[:6]}"

    async def _ensure_worktree_async(self) -> None:
        """Create this chat's worktree off the event loop, serialized so concurrent callers
        (auto-go-live + the first turn) never run ``git worktree add`` for the same branch at once."""
        if self._worktree_ready:
            return
        async with self._wt_lock:
            if self._worktree_ready:
                return
            await asyncio.to_thread(self._ensure_worktree)

    def _ensure_worktree(self) -> None:
        """Create (or reuse) this chat's private worktree and point ``self.git`` at it. Blocking
        git work — call via :meth:`_ensure_worktree_async`. Idempotent."""
        if self._worktree_ready:
            return
        branch = self.record.worktree_branch or self._chat_branch()
        path = self.repo_git.ensure_worktree(branch, base=self.record.base_branch)
        self.worktree_path = path
        self.git = GitService(path)
        self.record.worktree_branch = branch
        if not self.record.target_branch:
            self.record.target_branch = branch
        self._copy_workspace_skills(path)
        self._worktree_ready = True
        self.store.save(self.record)

    def _copy_workspace_skills(self, worktree: Path) -> None:
        """Give the agent the workspace's Agent Skills (.claude/skills/) even when they aren't
        committed — the worktree is a checkout that would otherwise only have committed ones.
        These copies are kept out of the live overlay and out of PRs (see changed_paths /
        _commit_pr_blocking)."""
        src = self.workspace / ".claude" / "skills"
        if not src.is_dir():
            return
        try:
            shutil.copytree(src, worktree / ".claude" / "skills", dirs_exist_ok=True)
        except OSError as exc:
            _log.warning("Couldn't copy workspace skills into the worktree: %s", exc)

    def cleanup_worktree(self) -> None:
        """Remove this chat's worktree and branch (on delete). Blocking; via asyncio.to_thread."""
        if self.worktree_path is not None:
            self.repo_git.discard_worktree(self.worktree_path, self.record.worktree_branch)

    def changed_paths(self) -> list[str]:
        """Every file this chat changed vs its base — committed (branch vs base) plus uncommitted —
        so the live overlay reflects the chat's full contribution even after a commit/PR."""
        if not self._worktree_ready:
            return []
        paths: set[str] = set()
        try:
            paths.update(c.path for c in self.git.status())
            base = self.record.base_branch or "HEAD"
            out = self.git.repo.git.diff("--name-only", f"{base}...HEAD")
            paths.update(line.strip() for line in out.splitlines() if line.strip())
        except Exception:  # noqa: BLE001
            pass
        # Never mirror agent config (.claude — incl. the skills we copy in) into the workspace; it
        # isn't the agent's work and reverting it could delete the user's own .claude files.
        return sorted(p for p in paths if not p.startswith(".claude/"))

    async def _ensure_adapter(self) -> None:
        if self.adapter is not None:
            return
        async with self._adapter_lock:
            if self.adapter is not None:  # another caller (e.g. warmup) won the race
                return
            await self._ensure_worktree_async()  # agent runs in the chat's worktree
            try:
                adapter = create_adapter(self.record.agent, self.worktree_path)
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
                adapter = create_adapter(self.record.agent, self.worktree_path)
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

        await self._run_turn(text)

    async def _run_turn(self, agent_input: str) -> None:
        """Run one agent turn: stream events, attribute changed files, persist the reply + resume
        id, mirror to the workspace if live, and return to idle. The user transcript entry and any
        per-turn adapter toggles are set up by the caller."""
        self._turn_text = []
        await self.send(P.Status(chat_id=self.chat_id, state="working"))

        # Snapshot the dirty set before the turn. Anything dirty *now* is pre-existing and must not
        # be attributed to the agent; anything that becomes dirty during the turn is the agent's
        # doing (also catches Bash/sed edits that don't surface as edit-tool events).
        before = self._dirty_paths()

        # Mark active only once we're committed to running the turn, and always clear it in the
        # finally — otherwise an exception around the loop would wedge the chat as perpetually
        # "working" and block Stop. The lock serializes turns *within this chat*; different chats
        # run in parallel (each in its own worktree).
        self._turn_active = True
        async with self._turn_lock:
            try:
                async for event in self.adapter.send(agent_input):  # type: ignore[union-attr]
                    await self._emit(event)
            except Exception as exc:  # noqa: BLE001
                await self.send(P.ErrorMessage(message=f"Agent run failed: {exc}", chat_id=self.chat_id))
            finally:
                self._turn_active = False

        for path in self._dirty_paths() - before:
            if path not in self.record.touched:
                self.record.touched.append(path)

        # If this chat is the live preview, mirror its (now-updated) changes into the workspace so
        # the dev server hot-reloads them.
        await self.live_mirror(self.chat_id, self.changed_paths())

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

    async def _on_update_branch(self, msg: P.UpdateBranch) -> None:
        """Merge the latest base ("main") into this chat's branch; if it conflicts, have the agent
        resolve and then complete the merge."""
        if self._turn_active:
            await self.send(P.ErrorMessage(message="The agent is still working — try again when it's idle.", chat_id=self.chat_id))
            return
        await self._ensure_worktree_async()
        base = self.record.base_branch or "main"
        await self.send(P.SystemNote(chat_id=self.chat_id, text=f"Updating from {base}…"))
        try:
            status, conflicts = await asyncio.to_thread(
                self.git.update_from_base, base, "origin", self.github_token
            )
        except GitError as exc:
            await self.send(P.ErrorMessage(message=f"Couldn't update from {base}: {exc}", chat_id=self.chat_id))
            return

        if status == "up_to_date":
            await self.send(P.SystemNote(chat_id=self.chat_id, text=f"Already up to date with {base}."))
            return
        if status == "merged":
            await self._after_merge_synced(f"Updated from {base} — no conflicts.")
            return

        # Conflicts: ask the agent to resolve them, then complete the merge.
        await self.send(P.SystemNote(
            chat_id=self.chat_id,
            text=f"Merging {base} hit conflicts in {len(conflicts)} file(s) — asking the agent to resolve…",
        ))
        try:
            await self._ensure_adapter()
        except Exception as exc:  # noqa: BLE001
            await self.send(P.ErrorMessage(message=f"Could not start agent to resolve conflicts: {exc}", chat_id=self.chat_id))
            return
        self.adapter.set_auto_approve(True)  # let it edit freely to resolve  # type: ignore[union-attr]
        self.adapter.set_mode(None); self.adapter.set_model(None); self.adapter.set_effort(None)  # type: ignore[union-attr]
        self.record.transcript.append({"kind": "user", "text": f"Resolve the merge conflicts from {base}."})
        self.store.save(self.record)
        await self.notify_chats()
        await self._run_turn(self._conflict_prompt(base, conflicts))

        # The agent edits files but doesn't `git add`, so check for leftover markers, not the index.
        remaining = await asyncio.to_thread(self.git.conflict_markers_remaining, conflicts)
        if remaining:
            await self.send(P.SystemNote(
                chat_id=self.chat_id,
                text=("Still " + str(len(remaining)) + " conflicted file(s): "
                      + ", ".join(remaining[:5]) + ". Ask the agent to finish, or resolve manually. "
                      "(The merge is in progress — Create PR is paused until it's resolved.)"),
            ))
            return
        try:
            await asyncio.to_thread(self.git.complete_merge, f"Merge {base} into {self.record.worktree_branch}")
        except GitError as exc:
            await self.send(P.ErrorMessage(message=f"Resolved the files but couldn't complete the merge: {exc}", chat_id=self.chat_id))
            return
        await self._after_merge_synced(f"Conflicts resolved — merge from {base} completed.")

    async def _after_merge_synced(self, note: str) -> None:
        """Shared tail after a successful update: refresh the file list, mirror if live, notify."""
        await self.live_mirror(self.chat_id, self.changed_paths())
        await self._refresh_files()
        self.store.save(self.record)
        await self.notify_chats()
        await self._send_file_changes()
        await self.send(P.SystemNote(chat_id=self.chat_id, text=note))

    @staticmethod
    def _conflict_prompt(base: str, conflicts: list[str]) -> str:
        files = "\n".join(f"- {p}" for p in conflicts)
        return (
            f"A merge of the latest `{base}` into this branch produced conflicts. Resolve ALL of "
            "them: open each conflicted file, integrate both sides correctly, and remove every "
            "conflict marker (`<<<<<<<`, `=======`, `>>>>>>>`). Make sure the result is correct and "
            "consistent. Do NOT run git commit or git merge — just edit the files to resolve.\n\n"
            f"Conflicted files:\n{files}"
        )

    async def _on_agent_response(self, msg: P.AgentResponse) -> None:
        if self.adapter is not None:
            await self.adapter.resolve_prompt(msg.request_id, msg.answer)

    async def _on_upload_file(self, msg: P.UploadFile) -> None:
        """Store an attached file under the workspace's gitignored .agentbridge/uploads/ and
        return the path the agent can read. Writing is blocking I/O, so it runs off the loop."""
        async def fail(reason: str) -> None:
            await self.send(P.FileUploaded(chat_id=self.chat_id, upload_id=msg.upload_id, ok=False, error=reason))

        await self._ensure_worktree_async()  # uploads go into the chat's worktree
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
        """Write upload bytes into the chat worktree's gitignored .agentbridge/uploads/<chat>/,
        de-duplicating the filename. Returns the worktree-relative path the agent can read.
        Blocking; call via asyncio.to_thread."""
        self._ensure_worktree()  # uploads live in the worktree the agent actually runs in
        root = self.worktree_path or self.workspace
        uploads = root / ".agentbridge" / "uploads" / self.chat_id
        uploads.mkdir(parents=True, exist_ok=True)
        self._ensure_agentbridge_gitignored(root)
        dest = _dedupe_path(uploads / _safe_filename(name))
        dest.write_bytes(data)
        return dest.relative_to(root).as_posix()

    @staticmethod
    def _ensure_agentbridge_gitignored(root: Path) -> None:
        """Make the whole .agentbridge/ directory invisible to git so uploads never show as
        changes or get swept into a PR — without touching the user's own .gitignore. A
        self-ignoring .gitignore inside the dir does exactly that."""
        marker = root / ".agentbridge" / ".gitignore"
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
        """Commit the chat's worktree changes onto its branch, push, and open a PR. The agent
        already worked in this chat's private worktree, so there's nothing to relocate."""
        await self._ensure_worktree_async()
        if await asyncio.to_thread(self.git.merge_in_progress):
            await self.send(P.ErrorMessage(
                message="A merge from main is still in progress — resolve it first, then create the PR.",
                chat_id=self.chat_id,
            ))
            return
        # Files the agent edited that are still uncommitted, plus anything already committed on the
        # branch (so a retry after a push failure — where the commit already landed — still works).
        touched = [p for p in self.record.touched if self.git.is_path_dirty(p)]
        has_committed = await asyncio.to_thread(self._has_commits_ahead)
        already_prd = any(e.get("kind") == "pr" for e in self.record.transcript)
        if not touched and (already_prd or not has_committed):
            msg = "No new agent changes since the last PR." if already_prd else "No agent changes to commit yet."
            await self.send(P.ErrorMessage(message=msg, chat_id=self.chat_id))
            return

        files_for_meta = touched or self.changed_paths()
        # Have the model write the title/description (isolated one-shot), with a deterministic
        # fallback. Only when the user didn't type both fields already.
        gen_title = gen_summary = None
        if not ((msg.title or "").strip() and (msg.body or "").strip()):
            gen_title, gen_summary = await self._model_pr(files_for_meta)
        title, body = self._pr_meta(
            msg.title, msg.body, files_for_meta, gen_title=gen_title, gen_summary=gen_summary, notes=msg.notes,
        )
        branch = self.record.worktree_branch
        try:
            # Blocking git + a synchronous GitHub call — run off the event loop.
            pr = await asyncio.to_thread(self._commit_pr_blocking, branch, title, body)
        except Exception as exc:  # noqa: BLE001 — surface any failure, but never lose the agent's work
            reason = str(exc) or exc.__class__.__name__
            await self.send(P.ErrorMessage(
                message=(f"Couldn't create the PR: {reason}\n"
                         "Your changes are safe in this chat — fix the issue and click Create PR again."),
                chat_id=self.chat_id,
            ))
            return

        self.record.touched = []  # committed — start fresh for any further edits in this chat
        self.record.target_branch = branch
        self.record.transcript.append({"kind": "branch", "branch": branch, "worktree_path": str(self.worktree_path)})
        self.record.transcript.append({"kind": "pr", "url": pr.url, "number": pr.number})
        await self._refresh_files()
        self.store.save(self.record)
        await self.notify_chats()
        await self.send(P.BranchCreated(chat_id=self.chat_id, branch=branch, worktree_path=str(self.worktree_path)))
        await self.send(P.PRCreated(chat_id=self.chat_id, url=pr.url, number=pr.number))
        await self._send_file_changes()

    def _has_commits_ahead(self) -> bool:
        """Whether the chat's branch has commits beyond its base (e.g. a prior PR attempt committed
        but failed to push). Blocking; call via asyncio.to_thread."""
        if not self._worktree_ready:
            return False
        base = self.record.base_branch or "HEAD"
        try:
            out = self.git.repo.git.rev_list("--count", f"{base}..HEAD").strip()
            return out.isdigit() and int(out) > 0
        except Exception:  # noqa: BLE001
            return False

    def _commit_pr_blocking(self, branch: str, title: str, body: str) -> PullRequest:
        """Commit the worktree's changes (no-op if a prior attempt already committed), push the
        chat's branch, and open the PR. Blocking; runs via asyncio.to_thread."""
        # Commit the agent's work, but never the .claude config we copy in (skills) or scratch.
        self.git.commit_all(title, exclude=[".claude", ".agentbridge"])
        if not self._has_commits_ahead():
            raise GitError("Nothing to commit for the agent's changes.")
        self.git.push(branch, token=self.github_token)
        return self.git.create_pull_request(title=title, head=branch, body=body, token=self.github_token)

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
            # Live-mirror this single file as the agent writes it (if this chat is the preview),
            # so the dev server hot-reloads mid-turn rather than only at the end.
            if rel:
                await self.live_mirror(self.chat_id, [rel])
        elif event.kind == "prompt" and event.request_id:
            await self.send(
                P.AgentPrompt(
                    chat_id=self.chat_id, request_id=event.request_id, prompt=event.text,
                    options=event.options, title=event.title, multi=event.multi,
                )
            )
        elif event.kind == "error":
            await self.send(P.ErrorMessage(message=event.text, chat_id=self.chat_id))

    def _format_attachments(self, attachments: list[str]) -> str:
        """Point the agent at files the user uploaded for this turn. Only list ones that actually
        landed under the uploads dir (defends against spoofed/relative paths from the client)."""
        if not attachments:
            return ""
        prefix = ".agentbridge/uploads/"
        root = self.worktree_path or self.workspace
        valid = [p for p in attachments if p.startswith(prefix) and (root / p).is_file()]
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
        """Normalize an agent-reported path to one relative to the chat's worktree (git pathspec)."""
        p = Path(path)
        if not p.is_absolute():
            return path
        root = self.worktree_path or self.workspace
        try:
            return str(p.relative_to(root))
        except ValueError:
            return None  # edited outside the worktree — not part of this chat's PR

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
        notes: str | None = None,
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
        if title and title[0].islower():   # PR titles read better capitalized
            title = title[0].upper() + title[1:]
        notes = (notes or "").strip()
        body = (user_body or "").strip()
        if body:
            body = f"{body}\n\n{notes}" if notes else body
        else:
            body = self._build_pr_body((gen_summary or summary), touched, notes)
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
    def _build_pr_body(cls, summary: str, touched: list[str], notes: str = "") -> str:
        parts: list[str] = []
        cleaned = cls._strip_summary_preamble(summary)
        if cleaned:
            parts.append("## Summary\n\n" + cleaned)
        if touched:
            files = "\n".join(f"- `{p}`" for p in sorted(touched))
            parts.append(f"## Files changed\n\n{files}")
        if notes and notes.strip():
            parts.append(notes.strip())   # user-configured PR notes, before the footer
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
    """Per-connection manager: owns the store and the live per-chat sessions.

    Each chat works in its own git worktree, so chats run in parallel. The workspace itself is a
    *preview surface*: exactly one chat can be "live" at a time, and its changes are mirrored into
    the workspace so the dev server hot-reloads them."""

    def __init__(self, workspace: Path, send: Send, github_token: str | None) -> None:
        self.workspace = workspace
        self.send = send
        self.github_token = github_token
        self.store = ChatStore(workspace)
        self.mcp = McpStore(workspace)
        self.git = GitService(workspace)  # validates the workspace is a git repo
        self.sessions: dict[str, Session] = {}
        self.live_chat_id: str | None = None      # the chat currently previewed in the workspace
        self.live_paths: set[str] = set()          # files overlaid into the workspace (to revert)
        self._live_lock = asyncio.Lock()           # serializes workspace overlay mutations

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
        elif msg.type == "go_live":
            await self._go_live(msg.chat_id)
        elif msg.type == "list_skills":
            await self._send_skills()
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
            notify_chats=self._send_chats,
            mcp_for_sdk=self.mcp.to_sdk,
            live_mirror=self.live_mirror,
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
        if self.live_chat_id is None:
            # A brand-new chat has nothing to overlay yet, so mark it live *eagerly* (don't await
            # worktree creation) — otherwise a fast first turn could finish before go-live set the
            # flag and its edits wouldn't mirror to the workspace.
            self.live_chat_id = record.id
            await self._send_live()
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
        await self._send_live()  # tell the client which chat (if any) is currently previewed
        if self.live_chat_id is None:   # nothing previewed yet -> preview this one
            await self._go_live(rec.id)
        asyncio.create_task(session.warmup())  # warm the (possibly resumed) agent in the background

    async def _delete(self, msg: P.DeleteChat) -> None:
        session = self.sessions.pop(msg.chat_id, None)
        if msg.chat_id == self.live_chat_id:
            await self._go_live(None)  # revert the overlay and clear the live preview
        if session is not None:
            await session.close()
            await asyncio.to_thread(session.cleanup_worktree)
        self.store.delete(msg.chat_id)
        await self.send(P.ChatDeleted(chat_id=msg.chat_id))
        await self._send_chats()

    # ------------------------------------------------------------------ #
    # Live preview (which chat the dev server mirrors)
    # ------------------------------------------------------------------ #

    async def _go_live(self, chat_id: str | None) -> None:
        """Make ``chat_id`` the live preview: revert the previous chat's overlay from the workspace,
        then overlay this chat's worktree changes so the dev server hot-reloads them."""
        async with self._live_lock:
            if self.live_paths:
                await asyncio.to_thread(self.git.discard_paths, sorted(self.live_paths))
                self.live_paths.clear()
            self.live_chat_id = None
            if chat_id:
                session = await self._get_session(chat_id)
                if session is None:
                    await self.send(P.ErrorMessage(message="That chat no longer exists.", chat_id=chat_id))
                else:
                    await session._ensure_worktree_async()
                    paths = await asyncio.to_thread(session.changed_paths)
                    if paths:
                        await asyncio.to_thread(session.git.copy_paths_to, self.workspace, paths)
                        self.live_paths = set(paths)
                    self.live_chat_id = chat_id
        await self._send_live()

    async def live_mirror(self, chat_id: str, paths: list[str]) -> None:
        """Mirror a chat's just-changed files into the workspace — but only if it's the live one."""
        if chat_id != self.live_chat_id or not paths:
            return
        session = self.sessions.get(chat_id)
        if session is None or not session._worktree_ready:
            return
        async with self._live_lock:
            if chat_id != self.live_chat_id:   # re-check under the lock
                return
            await asyncio.to_thread(session.git.copy_paths_to, self.workspace, paths)
            self.live_paths.update(paths)

    async def _send_live(self) -> None:
        await self.send(P.LiveChat(chat_id=self.live_chat_id))

    async def _end(self, msg: P.EndSession) -> None:
        if msg.chat_id:
            session = self.sessions.pop(msg.chat_id, None)
            if session is not None:
                await session.close()

    async def _send_chats(self) -> None:
        await self.send(P.Chats(chats=[P.ChatMeta(**m) for m in self.store.list_meta()]))

    async def _send_skills(self) -> None:
        from .skills import list_skills
        items = await asyncio.to_thread(list_skills, self.workspace)
        await self.send(P.Skills(skills=[P.SkillInfo(**s) for s in items]))

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
