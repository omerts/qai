"""Aider adapter — STUB.

Aider ships a Python API (``aider.coders.Coder``) as well as a CLI. To finish this
adapter:

  TODO: in ``start()``, build a ``Coder`` rooted at ``self.workspace`` (or spawn the
        ``aider`` CLI with ``--yes --message`` for one-shot turns), and in ``send()``
        stream the coder's output, mapping edited files to ``AgentEvent.file(...)``.
        Aider's ``Coder.run(with_message=...)`` returns the assistant text; for
        streaming, wire its IO callbacks.

Until then the adapter reports availability based on the ``aider`` binary but refuses
to run so the abstraction (and the frontend agent picker) still works end-to-end.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import AsyncIterator

from .base import AgentAdapter, AgentEvent, Capabilities, SessionContext


class AiderAdapter(AgentAdapter):
    name = "aider"
    label = "Aider"

    @classmethod
    def is_available(cls) -> bool:
        # The integration is not implemented yet, so report unavailable even if the
        # binary exists. Flip this to ``shutil.which("aider") is not None`` once done.
        _ = shutil  # referenced to document the intended check
        return False

    def capabilities(self) -> Capabilities:
        return Capabilities(streaming=True, interactive=False, edits_files=True)

    async def start(self, ctx: SessionContext) -> None:
        raise NotImplementedError(
            "Aider adapter is not implemented yet. See agentbridge/agents/aider.py TODO."
        )

    async def send(self, text: str) -> AsyncIterator[AgentEvent]:  # type: ignore[override]
        raise NotImplementedError
        yield  # pragma: no cover — makes this an async generator

    async def stop(self) -> None:
        return None
