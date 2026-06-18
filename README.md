# AgentBridge

Talk to a coding agent from inside your app's frontend and have it make real changes to
the dev environment the app is running in — without leaving the browser.

AgentBridge has three parts:

1. **Backend** (`backend/`) — a FastAPI server that orchestrates coding agents
   (Claude Code, Cursor, Aider, Copilot) against your local repo and handles git/PR.
2. **Widget** (`widget/`) — a Shadow-DOM chat bubble you drop into any React/Angular/Vue
   (or plain HTML) frontend with a single `<script>` tag. The shell is plain DOM; the
   message thread is rendered with [assistant-ui](https://www.assistant-ui.com/) (React).
3. **Session workflow** — chat with the agent while it edits your workspace in place, then
   click **Create PR** when done: AgentBridge commits the edits onto a fresh branch, opens a
   pull request, and resets your workspace back to a clean state.

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
pip install -e ".[claude,dev]"          # 'claude' pulls the Claude Agent SDK; omit if unused

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
| `AGENTBRIDGE_WORKTREE_DIR`| `<repo>/../.agentbridge-worktrees` | Where branch worktrees are created |
| `AGENTBRIDGE_STATE_DIR`  | `~/.agentbridge` | Where chat history is persisted (per workspace) |
| `AGENTBRIDGE_CLAUDE_SETTING_SOURCES` | `user,project` | Which Claude Code settings to load from disk (`local` omitted so no `.claude` files are written into your workspace; empty = CLI default) |
| `GITHUB_TOKEN`/`GH_TOKEN`| —             | Push + PR creation over HTTPS (falls back to `gh auth token`); no ssh needed |

### Run with Docker (alternative to step 1)

```bash
cp .env.example .env          # set WORKSPACE (repo to edit), ANTHROPIC_API_KEY, GITHUB_TOKEN
docker compose up --build     # server on http://localhost:8000, WS at ws://localhost:8000/ws
```

The image bundles git and the Claude Code CLI. Your repo is bind-mounted at `/workspace`
(the agent edits it and runs git there). See [docker-compose.yml](docker-compose.yml) and
[backend/Dockerfile](backend/Dockerfile).

**Auth with your Claude subscription (no API key):** run `claude setup-token` on your host
and put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN` (leave `ANTHROPIC_API_KEY` empty).
If you run the backend *directly* on a machine where you're already logged into Claude Code,
no token is needed — the CLI reuses your login automatically (just keep `ANTHROPIC_API_KEY`
unset so it doesn't fall back to API billing).

### 2. Build & embed the widget

```bash
cd widget && npm install && npm run build   # produces widget/dist/agentbridge-widget.js
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
| Claude Code  | `claude-agent-sdk` (`ClaudeSDKClient`)   | ✅ implemented |
| Cursor       | `cursor-agent` CLI (`stream-json`)       | ✅ implemented |
| Aider        | Python `Coder` API                       | 🚧 stub        |
| Copilot      | `gh copilot` / Copilot CLI               | 🚧 stub        |

**Interactive approvals.** Claude Code runs in `default` permission mode: when it wants to
edit/write/run a command, the widget shows an **Allow / Deny** card and the agent blocks
until you answer (the reply is fed back into the SDK's `can_use_tool` callback). Cursor runs
headless (`--print --force --trust`) so it doesn't block on approval prompts; it streams
incremental output via `stream-json --stream-partial-output` and keeps multi-turn context
through a `create-chat` id passed to `--resume`.

**Auto-approve (default on).** The widget has a shield toggle (next to the inspect button)
that auto-approves the agent's routine actions so it can work without stopping for every
edit. When on, file edits and ordinary shell commands run without a prompt — but commands
that look destructive or hard to undo (`rm -rf`, `sudo`, `git push --force`,
`git reset --hard`, `curl … | sh`, recursive `chmod`/`chown`, disk writes, `shutdown`, …)
still surface an **Allow / Deny** card. Turn the toggle off to confirm every action. The
preference is per-browser (`localStorage`) and sent with each message (`auto_approve`); it
only affects Claude Code (Cursor is already headless).

Each agent implements the `AgentAdapter` contract in
[`backend/agentbridge/agents/base.py`](backend/agentbridge/agents/base.py). Add a new agent
by writing one adapter and registering it in `agents/registry.py`.

**Workspace settings.** Agents run in your workspace and honor its own configuration, so the
rules/permissions/tools/MCP servers you've already set up apply:

- **Claude Code** loads the workspace's `.claude/settings.json`, `.mcp.json`, hooks, custom
  agents, and `CLAUDE.md`, plus your user settings in `~/.claude`. (In SDK/`--print` mode the
  CLI doesn't read filesystem settings unless asked, so AgentBridge passes
  `--setting-sources user,project`.) It **deliberately omits** the `local` source
  (`.claude/settings.local.json`) — that's a per-user, machine-local permission cache the CLI
  *writes* to, and excluding it keeps AgentBridge from reading or **creating any `.claude`
  file in your workspace**; approvals flow through the widget's Allow/Deny card at runtime
  instead. Tune via `AGENTBRIDGE_CLAUDE_SETTING_SOURCES` — e.g. add `local` back to honor the
  workspace's local settings, `project` to ignore your global user settings, or empty for the
  CLI default. Permission rules in the loaded settings are applied first; the Allow/Deny card
  only appears for actions they don't already decide.
- **Cursor** runs in the workspace too, so it picks up `.cursor/rules`, `.cursorrules`, and
  `AGENTS.md` automatically.

> In Docker, the workspace's `.claude`/`.cursor` come from the bind-mounted repo; the `user`
> source reads `/root/.claude` (the `agentbridge-claude-home` volume).

## Workflow

1. Open the bubble → pick an agent → a session starts **on your current branch**.
2. Describe a change in chat → the agent streams its work; edited files show in the footer.
3. Click **Create PR** → AgentBridge commits the edits onto a fresh branch, pushes, opens a
   GitHub PR (the link appears in chat), and resets your workspace back to a clean state.

### Edit in place, branch at PR time (hot reload works)

The agent edits files **directly in your workspace directory** the whole time it's working,
so a dev server running there with hot reload shows its changes **live** — no extra setup.

Your workspace's checked-out branch is never switched or committed to. When you click
**Create PR**, AgentBridge derives a branch name, creates a
[git worktree](https://git-scm.com/docs/git-worktree) for it, relocates your in-place edits
onto it, and commits/pushes from there — then opens the PR. After that the workspace is
**reset** back to a clean state on its original branch.

> Worktrees are created lazily at PR time under `AGENTBRIDGE_WORKTREE_DIR` (default: a
> `.agentbridge-worktrees` folder beside your repo; a persistent `/worktrees` volume in
> Docker). Because the agent works in the workspace, its conversation is never reset.

### Multiple chats & history

The widget runs many chats over one connection. Use **+** to start a chat and **☰** to
browse, reopen, or delete previous ones. State is persisted on the backend under
`AGENTBRIDGE_STATE_DIR` (per workspace), so:

- Your **selected agent** and **last open chat** are restored after a page refresh
  (kept in the browser's `localStorage`).
- Chat **transcripts** survive refreshes and server restarts, and are replayed when you
  reopen a chat.
- Reopening a chat **resumes the agent's context** — Claude via its `resume` session id,
  Cursor via `--resume` — so it continues where it left off rather than starting cold.
  (Resuming needs the agent CLI's own stored conversation; in Docker that's kept on the
  `agentbridge-claude-home` volume. If it can't be resumed, the chat continues fresh and
  says so.)

Because every chat edits the same workspace, agent turns are **serialized** — only one runs
at a time across all chats.

### What the agent sees (page context)

With every message the widget attaches lightweight **browser context** to help the agent:
the current route/URL, the detected framework + version (React / Vue / Angular), the page
title, and a best-effort list of components on the page. Use the **crosshair button** to
turn on *inspect mode*, then click any element on your app — its tag, CSS selector, text,
and (where resolvable) the owning component name and source file are attached to your next
message as context.

## Development

```bash
cd backend && . .venv/bin/activate
pytest                                   # protocol, git service, registry, session flow
```

## Project layout

```
backend/   FastAPI server, agent adapters, git service, WS protocol
widget/    Shadow-DOM widget (React/assistant-ui thread) + esbuild build + framework examples
```
