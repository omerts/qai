# AgentBridge backend

FastAPI server that orchestrates coding agents against a local git workspace and exposes a
single WebSocket endpoint (`/ws`) consumed by the widget. See the
[top-level README](../README.md) for the full picture.

## Install & run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[claude,dev]"
AGENTBRIDGE_WORKSPACE=/path/to/repo agentbridge
```

## Layout

| Module                | Responsibility                                             |
| --------------------- | ---------------------------------------------------------- |
| `main.py`             | FastAPI app, `/ws` endpoint, static widget hosting         |
| `protocol.py`         | Pydantic models for every WS message (the wire contract)   |
| `sessions.py`         | Per-connection session: agent ↔ git ↔ WS, branch suggestion|
| `git_service.py`      | branch / status / commit / push + GitHub PR creation       |
| `config.py`           | workspace + GitHub token discovery                         |
| `agents/base.py`      | `AgentAdapter` contract + `AgentEvent` stream              |
| `agents/registry.py`  | name → adapter mapping; capability/availability listing    |
| `agents/*.py`         | per-agent adapters (claude_code, cursor; aider/copilot stubs) |

## Tests

```bash
pytest
```
