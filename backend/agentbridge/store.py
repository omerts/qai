"""On-disk persistence for chats: metadata, transcript, and agent resume handles.

Each chat is one JSON file under a per-workspace state directory, so history survives
page refreshes and server restarts. The state root is ``AGENTBRIDGE_STATE_DIR`` (default
``~/.agentbridge``), namespaced by a hash of the workspace path so different repos don't
mix. The transcript stored here is what the widget replays when you reopen a chat; the
``resume_id`` is the agent's native session/chat id used to *continue* the conversation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("agentbridge")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_dir_for(workspace: Path) -> Path:
    root = os.environ.get("AGENTBRIDGE_STATE_DIR")
    base = Path(root).expanduser() if root else Path.home() / ".agentbridge"
    key = hashlib.sha1(str(Path(workspace).resolve()).encode()).hexdigest()[:12]
    return base / key


@dataclass
class ChatRecord:
    """The full persisted state of one chat."""

    id: str
    agent: str
    title: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    base_branch: str | None = None
    target_branch: str | None = None
    #: The chat's dedicated branch — the agent works in a private git worktree on this branch, so
    #: chats run in parallel without touching each other or the workspace.
    worktree_branch: str | None = None
    #: Agent-native handle used to resume the conversation (Claude session id / Cursor chat id).
    resume_id: str | None = None
    #: Renderable transcript entries (see ``Session`` for the kinds it writes).
    transcript: list[dict] = field(default_factory=list)
    #: Last known changed-file summary, for the footer on reopen.
    files: list[dict] = field(default_factory=list)
    #: Workspace-relative paths the agent edited this session. Only these are committed when
    #: opening a PR, so the user's unrelated pre-existing changes are left untouched.
    touched: list[str] = field(default_factory=list)

    def meta(self) -> dict:
        """Lightweight summary for the chat list."""
        messages = sum(1 for e in self.transcript if e.get("kind") in ("user", "agent"))
        return {
            "id": self.id,
            "title": self.title or "New chat",
            "agent": self.agent,
            "updated_at": self.updated_at,
            "message_count": messages,
        }


class ChatStore:
    def __init__(self, workspace: Path) -> None:
        self.dir = state_dir_for(workspace) / "chats"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, chat_id: str) -> Path:
        return self.dir / f"{chat_id}.json"

    def create(self, agent: str, title: str | None = None) -> ChatRecord:
        record = ChatRecord(id=uuid.uuid4().hex[:12], agent=agent, title=title)
        self.save(record)
        return record

    def load(self, chat_id: str) -> ChatRecord | None:
        path = self._path(chat_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt/unreadable chat file shouldn't crash the listing, but log it so the loss
            # is diagnosable rather than the chat just silently vanishing.
            _log.warning("Skipping unreadable chat file %s: %s", path, exc)
            return None
        # Tolerate older/newer files by filtering to known fields.
        known = ChatRecord.__dataclass_fields__  # type: ignore[attr-defined]
        return ChatRecord(**{k: v for k, v in data.items() if k in known})

    def save(self, record: ChatRecord) -> None:
        record.updated_at = _now()
        path = self._path(record.id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(record), indent=2))
        tmp.replace(path)  # atomic on the same filesystem

    def delete(self, chat_id: str) -> bool:
        path = self._path(chat_id)
        if path.is_file():
            path.unlink()
            return True
        return False

    def list_meta(self) -> list[dict]:
        """Chat summaries, most-recently-updated first."""
        records: list[ChatRecord] = []
        for path in self.dir.glob("*.json"):
            rec = self.load(path.stem)
            if rec is not None:
                records.append(rec)
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return [r.meta() for r in records]
