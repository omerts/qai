"""Cursor adapter, driven via the headless ``cursor-agent`` CLI.

Validated against ``cursor-agent`` 2026.03.x. Relevant flags (from ``cursor-agent --help``):

  --print / -p                 non-interactive; has access to write & shell tools
  --output-format <fmt>        text | json | stream-json
  --stream-partial-output      stream text deltas (needs --print + stream-json)
  --resume [chatId]            resume a specific chat (multi-turn continuity)
  --model <model>              model to use (e.g. gpt-5, sonnet-4.5); see --list-models
  --force / --yolo             allow commands unless explicitly denied
  --trust                      trust the workspace without prompting (headless only)
  --approve-mcps               auto-approve MCP servers (headless can't answer a prompt)
  create-chat                  command that prints a new chat id

Why ``--force`` and ``--trust`` matter: without them a headless run can *block* waiting
on an approval/trust prompt that no one can answer over our pipe, hanging the turn. Cursor
in ``--print`` mode does not ask us mid-run, so this adapter is non-interactive.

Multi-turn continuity: we mint a chat id with ``create-chat`` at session start and pass it
to every turn via ``--resume``. If ``create-chat`` is unavailable we capture the
``session_id`` emitted in the stream and reuse it for subsequent turns.

Parity with the Claude adapter, within what the headless CLI exposes:
- Model selection — ``--model`` (see :meth:`models`); no effort/plan mode headless.
- MCP plugins — the user's enabled servers are written to ``<worktree>/.cursor/mcp.json``
  (Cursor's native discovery path) at session start, and ``--approve-mcps`` skips the
  approval prompt. The file is kept out of PRs (``.cursor`` is excluded in sessions.py).
- Project instructions — Cursor reads ``AGENTS.md``/``.cursor/rules``/``.cursorrules`` but
  NOT ``CLAUDE.md``. If the workspace only has ``CLAUDE.md`` we bridge it in as a one-time
  preamble on the first turn so the agent gets the same guidance (see :meth:`_read_preamble`).
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

    #: Selectable models. Cursor's own source of truth is ``cursor-agent --list-models`` (which
    #: needs auth + network, so we don't shell out to it on every connect); this curated set
    #: tracks Cursor's documented CLI model aliases. ``""`` => Cursor's built-in auto default.
    _MODELS = [
        {"id": "", "label": "Auto (default)"},
        {"id": "gpt-5", "label": "GPT-5"},
        {"id": "sonnet-4.5", "label": "Sonnet 4.5"},
        {"id": "sonnet-4.5-thinking", "label": "Sonnet 4.5 (thinking)"},
        {"id": "opus-4.1", "label": "Opus 4.1"},
        {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
    ]

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self._chat_id: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._interrupted: bool = False
        self._model_id: str | None = None  # selected model id (None/"" => Cursor default)
        self._preamble: str = ""           # CLAUDE.md bridge text to inject on the first turn

    @classmethod
    def is_available(cls) -> bool:
        return shutil.which(_BINARY) is not None

    def capabilities(self) -> Capabilities:
        # cursor-agent --print is non-interactive (no mid-run prompts to us).
        return Capabilities(streaming=True, interactive=False, edits_files=True)

    def models(self) -> list[dict[str, str]]:
        return [dict(m) for m in self._MODELS]

    def set_model(self, model: str | None) -> None:
        self._model_id = model or None

    async def start(self, ctx: SessionContext) -> None:
        if not self.is_available():
            raise RuntimeError(
                f"'{_BINARY}' not found on PATH. Install the Cursor CLI to use this agent."
            )
        # Hand the user's enabled MCP plugins to Cursor via its native discovery file, and bridge
        # CLAUDE.md into the first turn if that's the only project-instructions file present.
        self._write_mcp_config(ctx.mcp_servers or {})
        self._preamble = self._read_preamble()
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

    def _write_mcp_config(self, sdk_servers: dict) -> None:
        """Translate the user's enabled MCP servers (SDK ``{name: cfg}`` shape) into Cursor's
        ``<worktree>/.cursor/mcp.json`` so ``cursor-agent`` discovers them natively. Merges into
        any existing file rather than clobbering the workspace's own servers. No-op when empty."""
        translated: dict[str, dict] = {}
        for name, cfg in (sdk_servers or {}).items():
            if isinstance(cfg, dict):
                out = self._sdk_to_cursor(cfg)
                if out is not None:
                    translated[name] = out
        if not translated:
            return
        path = self.workspace / ".cursor" / "mcp.json"
        existing: dict = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
                    existing = data["mcpServers"]
            except (json.JSONDecodeError, OSError):
                existing = {}
        merged = {**existing, **translated}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"mcpServers": merged}, indent=2))
        except OSError:
            pass  # a missing MCP file just means those plugins are unavailable this session

    @staticmethod
    def _sdk_to_cursor(cfg: dict) -> dict | None:
        """One server, SDK shape -> Cursor ``mcp.json`` shape. Cursor infers transport from the
        keys present (``command`` => stdio, ``url`` => remote), so we drop the ``type`` field."""
        if cfg.get("type", "stdio") == "stdio":
            command = cfg.get("command")
            if not command:
                return None
            out: dict = {"command": command}
            if cfg.get("args"):
                out["args"] = list(cfg["args"])
            if cfg.get("env"):
                out["env"] = dict(cfg["env"])
            return out
        url = cfg.get("url")
        if not url:
            return None
        out = {"url": url}
        if cfg.get("headers"):
            out["headers"] = dict(cfg["headers"])
        return out

    def _read_preamble(self) -> str:
        """Bridge ``CLAUDE.md`` into Cursor, which doesn't read it natively. Skipped when the
        workspace already has an ``AGENTS.md`` (Cursor reads that), so we never duplicate guidance."""
        if (self.workspace / "AGENTS.md").is_file():
            return ""
        claude_md = self.workspace / "CLAUDE.md"
        if not claude_md.is_file():
            return ""
        try:
            return claude_md.read_text(errors="replace").strip()
        except OSError:
            return ""

    def _build_command(self, text: str) -> list[str]:
        cmd = [
            _BINARY,
            "--print",
            "--output-format", "stream-json",
            "--stream-partial-output",
            "--force",          # don't block on command-approval prompts
            "--trust",          # don't block on workspace-trust prompt (headless only)
            "--approve-mcps",   # don't block on MCP-approval prompts
        ]
        if self._model_id:
            cmd += ["--model", self._model_id]
        if self._chat_id:
            cmd += ["--resume", self._chat_id]
        cmd += ["--", text]  # terminate options; prompt is positional
        return cmd

    async def send(self, text: str) -> AsyncIterator[AgentEvent]:  # type: ignore[override]
        # On the first turn, prepend the bridged CLAUDE.md guidance (consumed once).
        if self._preamble:
            text = (
                "Project instructions from CLAUDE.md (treat these as you would AGENTS.md):\n\n"
                f"{self._preamble}\n\n---\n\n{text}"
            )
            self._preamble = ""
        cmd = self._build_command(text)
        self._interrupted = False
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
        self._proc = proc
        stderr_task = asyncio.create_task(self._drain(proc.stderr))

        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                for event in self._parse_line(line.decode(errors="replace")):
                    yield event

            await proc.wait()
            stderr = (await stderr_task).strip()
            # A user-requested stop kills the process — that's expected, not an error.
            if not self._interrupted and proc.returncode not in (0, None):
                yield AgentEvent.error(f"Cursor agent failed: {stderr or f'exit code {proc.returncode}'}")
                return
            yield AgentEvent.done()
        finally:
            # On the normal path stderr_task is already awaited above; if we're unwinding early
            # (consumer closed the generator, or readline raised) it's still running — cancel it
            # so the drain coroutine doesn't outlive the turn.
            if not stderr_task.done():
                stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
            self._proc = None

    async def interrupt(self) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        self._interrupted = True
        try:
            proc.terminate()
            return True
        except ProcessLookupError:
            return False

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
        # Terminate a still-running process so tearing down the session (e.g. on disconnect)
        # never orphans a cursor-agent subprocess.
        proc = self._proc
        if proc is not None and proc.returncode is None:
            self._interrupted = True
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        self._chat_id = None
