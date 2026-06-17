"""GitHub Copilot adapter — STUB.

Copilot's coding agent is reachable via the ``gh copilot`` CLI extension (suggest/explain)
and, for agentic edits, the Copilot CLI / API. To finish this adapter:

  TODO: in ``start()``, verify ``gh copilot`` (or the standalone ``copilot`` CLI) is
        authenticated, then in ``send()`` spawn it for one turn against ``self.workspace``
        and stream stdout, mapping edited files to ``AgentEvent.file(...)``. Note that
        ``gh copilot suggest`` is advisory-only; the agentic edit flow requires the
        Copilot CLI agent mode.

Until then the adapter reports unavailable so the picker shows it greyed out.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import AsyncIterator

from .base import AgentAdapter, AgentEvent, Capabilities, SessionContext


class CopilotAdapter(AgentAdapter):
    name = "copilot"
    label = "GitHub Copilot"

    @classmethod
    def is_available(cls) -> bool:
        # Not implemented yet. Once built, check e.g.:
        #   shutil.which("copilot") or (gh + copilot extension installed)
        _ = shutil
        return False

    def capabilities(self) -> Capabilities:
        return Capabilities(streaming=True, interactive=False, edits_files=True)

    async def start(self, ctx: SessionContext) -> None:
        raise NotImplementedError(
            "Copilot adapter is not implemented yet. See agentbridge/agents/copilot.py TODO."
        )

    async def send(self, text: str) -> AsyncIterator[AgentEvent]:  # type: ignore[override]
        raise NotImplementedError
        yield  # pragma: no cover — makes this an async generator

    async def stop(self) -> None:
        return None
