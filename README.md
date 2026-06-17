# AgentBridge

Talk to a coding agent from inside your app's frontend and have it make real changes to
the dev environment the app is running in — without leaving the browser.

AgentBridge has three parts:

1. **Backend** (`backend/`) — a FastAPI server that orchestrates coding agents
   (Claude Code, Cursor, Aider, Copilot) against your local repo and handles git/PR.
2. **Widget** (`widget/`) — a vanilla-JS chat bubble you drop into any React/Angular/Vue
   (or plain HTML) frontend with a single `<script>` tag.
3. **Session workflow** — chat with the agent, **branch when you decide to** (the agent
   only *suggests* branching), then open a pull request when done.

```
┌─────────────────┐   WebSocket    ┌──────────────────────────┐
│  Vanilla-JS     │ <============> │  FastAPI backend         │
│  chat widget    │   (JSON msgs)  │  (runs on dev machine)   │
│  (in host app)  │                │  Sessions · Git · Agents │
└─────────────────┘                └──────────────────────────┘
                                          │ runs agents + git in
                                          ▼ your repo (the workspace)
```

> **v1 scope:** local, single developer. No auth/multi-tenancy. PRs target GitHub.
> Claude Code and Cursor are fully wired; Aider and Copilot are stubs behind the same
> adapter interface (they appear in the picker as unavailable until implemented).

## Quickstart

### 1. Run the backend in your repo

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[claude,dev]"          # 'claude' pulls the Claude Code SDK; omit if unused

# Point it at the repo you want the agent to edit (defaults to the current directory):
AGENTBRIDGE_WORKSPACE=/path/to/your/repo agentbridge
# Server: http://127.0.0.1:8000  ·  WS: ws://127.0.0.1:8000/ws
```

Configuration (all optional, via env vars):

| Variable                 | Default       | Purpose                                  |
| ------------------------ | ------------- | ---------------------------------------- |
| `AGENTBRIDGE_WORKSPACE`  | cwd           | Repo the agent operates on               |
| `AGENTBRIDGE_HOST`       | `127.0.0.1`   | Bind host                                |
| `AGENTBRIDGE_PORT`       | `8000`        | Bind port                                |
| `GITHUB_TOKEN`/`GH_TOKEN`| —             | PR creation (falls back to `gh auth token`) |

### Run with Docker (alternative to step 1)

```bash
cp .env.example .env          # set WORKSPACE (repo to edit), ANTHROPIC_API_KEY, GITHUB_TOKEN
docker compose up --build     # server on http://localhost:8000, WS at ws://localhost:8000/ws
```

The image bundles git and the Claude Code CLI. Your repo is bind-mounted at `/workspace`
(the agent edits it and runs git there). See [docker-compose.yml](docker-compose.yml) and
[backend/Dockerfile](backend/Dockerfile).

### 2. Build & embed the widget

```bash
python widget/build.py                   # produces widget/dist/agentbridge-widget.js
```

```html
<script
  src="http://localhost:8000/widget/agentbridge-widget.js"
  data-server="ws://localhost:8000/ws"
></script>
```

Framework snippets live in [`widget/examples/`](widget/examples) (React, Vue, Angular,
plain HTML).

## Coding agents

| Agent        | Driver                                   | Status        |
| ------------ | ---------------------------------------- | ------------- |
| Claude Code  | `claude-code-sdk` (`ClaudeSDKClient`)    | ✅ implemented |
| Cursor       | `cursor-agent` CLI (`stream-json`)       | ✅ implemented |
| Aider        | Python `Coder` API                       | 🚧 stub        |
| Copilot      | `gh copilot` / Copilot CLI               | 🚧 stub        |

**Interactive approvals.** Claude Code runs in `default` permission mode: when it wants to
edit/write/run a command, the widget shows an **Allow / Deny** card and the agent blocks
until you answer (the reply is fed back into the SDK's `can_use_tool` callback). Cursor runs
headless (`--print --force --trust`) so it doesn't block on approval prompts; it streams
incremental output via `stream-json --stream-partial-output` and keeps multi-turn context
through a `create-chat` id passed to `--resume`.

Each agent implements the `AgentAdapter` contract in
[`backend/agentbridge/agents/base.py`](backend/agentbridge/agents/base.py). Add a new agent
by writing one adapter and registering it in `agents/registry.py`.

## Workflow

1. Open the bubble → pick an agent → a session starts **on your current branch**.
2. Describe a change in chat → the agent streams its work; edited files show in the footer.
3. The agent **offers** to branch when it starts editing on your base branch — you accept or
   ignore it. You can also click **Branch** at any time. Branching is always your call.
4. Click **Create PR** → AgentBridge commits, pushes, and opens a GitHub PR; the link
   appears in chat.

## Development

```bash
cd backend && . .venv/bin/activate
pytest                                   # protocol, git service, registry, session flow
```

## Project layout

```
backend/   FastAPI server, agent adapters, git service, WS protocol
widget/    vanilla-JS Shadow-DOM widget + build script + framework examples
```
