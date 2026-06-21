"""FastAPI application: the WebSocket endpoint and static hosting of the widget bundle.

Run it from inside (or pointed at) the repo you want the agent to work on:

    AGENTBRIDGE_WORKSPACE=/path/to/repo agentbridge
    # or
    cd /path/to/repo && python -m agentbridge.main
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__, protocol as P
from .config import get_settings
from .sessions import ChatHub

_log = logging.getLogger("agentbridge")

app = FastAPI(title="AgentBridge", version=__version__)

# The widget is embedded in arbitrary dev frontends, so allow cross-origin WS/HTTP.
# Safe here because v1 is local, single-developer.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> JSONResponse:
    settings = get_settings()
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "workspace": str(settings.workspace),
            "github_token": bool(settings.github_token),
        }
    )


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    settings = get_settings()

    # Serialize sends: messages are handled in concurrent tasks (so the receive loop keeps
    # reading while a long agent turn awaits an interactive prompt answer), and a Starlette
    # WebSocket must not be written from two coroutines at once.
    send_lock = asyncio.Lock()

    async def send(message: P.ServerMessage) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(message.model_dump()))

    try:
        hub = ChatHub(settings.workspace, send, settings.github_token)
    except Exception as exc:  # noqa: BLE001 — e.g. workspace is not a git repo
        await send(P.ErrorMessage(message=str(exc)))
        await websocket.close()
        return

    tasks: set[asyncio.Task] = set()

    async def dispatch(msg: P.ClientMessage) -> None:
        try:
            await hub.handle(msg)
        except Exception as exc:  # noqa: BLE001 — never let one handler kill the connection
            _log.exception("Handler error for %s", getattr(msg, "type", "?"))
            await send(P.ErrorMessage(message=f"Handler error: {exc}"))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                msg = P.parse_client_message(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                await send(P.ErrorMessage(message=f"Invalid message: {exc}"))
                continue
            # Handle concurrently so that, e.g., an `agent_response` can be received and
            # routed while a `user_message` turn is still blocked on a permission prompt.
            task = asyncio.create_task(dispatch(msg))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        # Let the cancelled handlers actually unwind before we tear the adapter down, so adapter
        # cleanup doesn't race a still-running turn.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await hub.close()


class _NoCacheStatic(StaticFiles):
    """StaticFiles that forces revalidation. The widget is iterated on constantly and embedded
    via a fixed <script> URL, so without this browsers serve a stale bundle after a rebuild
    (StaticFiles sets ETag/Last-Modified but no Cache-Control, so heuristic caching kicks in).
    ``no-cache`` means "always revalidate" — a cheap 304 when unchanged, fresh bytes after a
    rebuild — so a plain page refresh always loads the current widget."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


# Serve the built widget bundle (if present) at /widget for easy embedding/testing.
_WIDGET_DIST = Path(__file__).resolve().parents[2] / "widget" / "dist"
_WIDGET_SRC = Path(__file__).resolve().parents[2] / "widget" / "src"

for mount, directory in (("/widget", _WIDGET_DIST), ("/widget-src", _WIDGET_SRC)):
    bundle = directory / "agentbridge-widget.js"
    if directory.is_dir():
        app.mount(mount, _NoCacheStatic(directory=str(directory)), name=mount.strip("/"))
        # Make a 404 diagnosable: log that the mount exists and whether the bundle is there.
        _log.info("Serving %s from %s (bundle present: %s)", mount, directory, bundle.is_file())
    elif mount == "/widget":
        _log.warning(
            "Widget NOT served: %s does not exist, so GET %s/agentbridge-widget.js will 404. "
            "Build it with `cd widget && npm run build` (or rebuild the Docker image so it COPYs "
            "widget/dist).", directory, mount,
        )


def run() -> None:
    """Console-script entry point (``agentbridge``)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    run()
