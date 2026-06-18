"""Cursor adapter, driven via the headless ``cursor-agent`` CLI.

Validated against ``cursor-agent`` 2026.03.x. Relevant flags (from ``cursor-agent --help``):

  --print / -p                 non-interactive; has access to write & shell tools
  --output-format <fmt>        text | json | stream-json
  --stream-partial-output      stream text deltas (needs --print + stream-json)
  --resume [chatId]            resume a specific chat (multi-turn continuity)
  --force / --yolo             allow commands unless explicitly denied
  --trust                      trust the workspace without prompting (headless only)
  create-chat                  command that prints a new chat id

Why ``--force`` and ``--trust`` matter: without them a headless run can *block* waiting
on an approval/trust prompt that no one can answer over our pipe, hanging the turn. Cursor
in ``--print`` mode does not ask us mid-run, so this adapter is non-interactive.

Multi-turn continuity: we mint a chat id with ``create-chat`` at session start and pass it
to every turn via ``--resume``. If ``create-chat`` is unavailable we capture the
``session_id`` emitted in the stream and reuse it for subsequent turns.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import AsyncIterator

from .base import AgentAdapter, AgentEvent, Capabilities, SessionContext

_BINARY = "cursor-agent"
_FILE_TOOLS = {"edit", "write", "create", "multiedit", "str_replace", "apply_patch", "search_replace"}


class CursorAdapter(AgentAdapter):
    name = "cursor"
    label = "Cursor"
    theme = {"accent": "#111827", "accentFg": "#ffffff"}  # Cursor near-black

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self._chat_id: str | None = None

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which(_BINARY) is not None

    def capabilities(self) -> Capabilities:
        # cursor-agent --print is non-interactive (no mid-run prompts to us).
        return Capabilities(streaming=True, interactive=False, edits_files=True)

    async def start(self, ctx: SessionContext) -> None:
        if not self.is_available():
            raise RuntimeError(
                f"'{_BINARY}' not found on PATH. Install the Cursor CLI to use this agent."
            )
        # Resume a prior chat if we have its id; otherwise mint a fresh one.
        self._chat_id = ctx.resume or await self._create_chat()

    def resume_handle(self) -> str | None:
        return self._chat_id

    async def _create_chat(self) -> str | None:
        """Mint a fresh chat id via ``cursor-agent create-chat`` (best-effort)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                _BINARY, "create-chat",
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        chat_id = out.decode(errors="replace").strip().splitlines()[-1].strip() if out.strip() else None
        return chat_id or None

    def _build_command(self, text: str) -> list[str]:
        cmd = [
            _BINARY,
            "--print",
            "--output-format", "stream-json",
            "--stream-partial-output",
            "--force",   # don't block on command-approval prompts
            "--trust",   # don't block on workspace-trust prompt (headless only)
        ]
        if self._chat_id:
            cmd += ["--resume", self._chat_id]
        cmd += ["--", text]  # terminate options; prompt is positional
        return cmd

    async def send(self, text: str) -> AsyncIterator[AgentEvent]:  # type: ignore[override]
        cmd = self._build_command(text)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            yield AgentEvent.error(f"Failed to launch {_BINARY}: {exc}")
            return

        assert proc.stdout is not None and proc.stderr is not None
        stderr_task = asyncio.create_task(self._drain(proc.stderr))

        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            for event in self._parse_line(line.decode(errors="replace")):
                yield event

        await proc.wait()
        stderr = (await stderr_task).strip()
        if proc.returncode not in (0, None):
            yield AgentEvent.error(f"Cursor agent failed: {stderr or f'exit code {proc.returncode}'}")
            return
        yield AgentEvent.done()

    @staticmethod
    async def _drain(reader: asyncio.StreamReader) -> str:
        chunks = []
        while True:
            line = await reader.readline()
            if not line:
                break
            chunks.append(line.decode(errors="replace"))
        return "".join(chunks)

    def _parse_line(self, line: str) -> list[AgentEvent]:
        """Parse one NDJSON stream-json line into events.

        Tolerant by design: if a line isn't JSON (older versions / text leakage) we emit it
        verbatim as a chunk so output is never silently dropped.
        """
        line = line.strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return [AgentEvent.chunk(line + "\n")]

        # Capture a session/chat id for resuming subsequent turns.
        sid = obj.get("session_id") or obj.get("chat_id") or obj.get("chatId")
        if sid and not self._chat_id:
            self._chat_id = str(sid)

        etype = obj.get("type")
        # Skip the final aggregate result to avoid duplicating streamed deltas.
        if etype == "result":
            return []

        events: list[AgentEvent] = []
        events += self._text_events(obj)
        events += self._file_events(obj)
        return events

    @staticmethod
    def _text_events(obj: dict) -> list[AgentEvent]:
        # Direct delta shapes.
        for key in ("delta", "text"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return [AgentEvent.chunk(val)]
        # Assistant message with nested content blocks.
        message = obj.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            blocks = content if isinstance(content, list) else [content]
            out = []
            for block in blocks:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    out.append(AgentEvent.chunk(block["text"]))
            return out
        return []

    @staticmethod
    def _file_events(obj: dict) -> list[AgentEvent]:
        name = str(obj.get("name") or obj.get("tool") or "").lower()
        args = obj.get("input") or obj.get("args") or obj.get("arguments") or {}
        if name in _FILE_TOOLS and isinstance(args, dict):
            path = args.get("file_path") or args.get("path") or args.get("target_file")
            if path:
                return [AgentEvent.file(str(path))]
        return []

    async def stop(self) -> None:
        self._chat_id = None
