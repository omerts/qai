# AgentBridge

**Talk to a coding agent from inside your app's frontend and have it make real changes to the dev environment the app is running in — without leaving the browser.**

AgentBridge drops a chat bubble into any web app. You describe a change (optionally clicking the exact element you mean), a coding agent — Claude Code or Cursor — edits your local repo in place so your dev server hot-reloads live, and when you're happy you click **Create PR** to open a pull request with just the agent's changes.

```
┌─────────────────┐   WebSocket    ┌──────────────────────────┐
│  Chat widget    │ <============> │  FastAPI backend         │
│  (in host app)  │   (JSON msgs)  │  (runs on dev machine)   │
│  Shadow DOM      │                │  Sessions · Git · Agents │
└─────────────────┘                └──────────────────────────┘
                                          │ runs agents + git in
                                          ▼ your repo (the workspace)
```

### Three parts

1. **Backend** (`backend/`) — a FastAPI server that orchestrates coding agents against your local repo and handles git/PR.
2. **Widget** (`widget/`) — a Shadow-DOM chat bubble you embed with a single `<script>` tag. The shell is plain DOM; the message thread renders with [assistant-ui](https://www.assistant-ui.com/) (React).
3. **Session workflow** — chat while the agent edits your workspace in place, then click **Create PR** to commit _only the agent's edits_ onto a fresh branch and open a pull request — your other uncommitted work is left untouched.

> **Scope:** local, single developer. No auth/multi-tenancy. PRs target GitHub. Claude Code and Cursor are fully wired; Aider and Copilot are stubs behind the same adapter interface (they appear in the picker as unavailable until implemented).

---

## Prerequisites

- **Git** and a **GitHub repo** you want to edit (PR creation is GitHub-only for now).
- A coding agent on the machine running the backend:
  - **Claude Code** — the `@anthropic-ai/claude-code` CLI + a Claude subscription token or `ANTHROPIC_API_KEY`, or
  - **Cursor** — the `cursor-agent` CLI.
- For a **direct (non-Docker)** run: **Python 3.10+** and **Node 18+** (to build the widget).
- For **Docker**: just Docker + Docker Compose (the image bundles git, Node, and the Claude Code CLI).

---

## Quickstart (Docker — recommended)

```bash
git clone https://github.com/omerts/qai.git agentbridge && cd agentbridge
cp .env.example .env        # set WORKSPACE + Claude auth + GITHUB_TOKEN (see below)
docker compose up --build   # http://localhost:8000  ·  ws://localhost:8000/ws
```

Then embed the widget in your app (the bundle is served by the backend):

```html
<script
  src="http://localhost:8000/widget/agentbridge-widget.js"
  data-server="ws://localhost:8000/ws"
></script>
```

Open your app, click the chat bubble, pick an agent, and start. Framework snippets (React, Vue, Angular, plain HTML) live in [`widget/examples/`](widget/examples).

### `.env` essentials

| Variable                    | Required | Purpose                                                                                                                          |
| --------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `WORKSPACE`                 | ✅       | Absolute path to the git repo the agent edits (bind-mounted at `/workspace`).                                                    |
| `CLAUDE_CODE_OAUTH_TOKEN`   | one of   | Claude subscription token from `claude setup-token` (no API billing).                                                            |
| `ANTHROPIC_API_KEY`         | one of   | Use API billing instead. If both are set, the API key wins.                                                                      |
| `GITHUB_TOKEN` / `GH_TOKEN` | for PRs  | Token with `repo` scope — used to push and open PRs over HTTPS (no ssh needed).                                                  |
| `PUID` / `PGID`             | optional | Run as your host user so the agent's edits stay owned by you. Defaults to `1000:1000`; set to your `id -u`/`id -g` if different. |

See [`.env.example`](.env.example) for the full list (port, worktree/state dirs, Claude setting sources, sandbox toggle).

> **Auth without an API key:** run `claude setup-token` on your host and put the result in `CLAUDE_CODE_OAUTH_TOKEN` (leave `ANTHROPIC_API_KEY` empty). If you run the backend _directly_ on a machine already logged into Claude Code, no token is needed — keep `ANTHROPIC_API_KEY` unset so it reuses your login instead of API billing.

> **Reclaiming file ownership:** if an earlier run (as root) left root-owned files in your repo, fix them once with `sudo chown -R $(id -u):$(id -g) /path/to/your/repo`, and make sure `PUID`/`PGID` match your user.

---

## Quickstart (direct, no Docker)

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[claude,dev]"          # 'claude' pulls the Claude Agent SDK; omit if unused

# Point it at the repo you want the agent to edit (defaults to the current directory):
AGENTBRIDGE_WORKSPACE=/path/to/your/repo agentbridge
# Server: http://127.0.0.1:8000  ·  WS: ws://127.0.0.1:8000/ws
```

Build & serve the widget:

```bash
cd widget && npm install && npm run build   # produces widget/dist/agentbridge-widget.js
```

Configuration (all optional, via env vars):

| Variable                             | Default                            | Purpose                                                                                                                                   |
| ------------------------------------ | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `AGENTBRIDGE_WORKSPACE`              | cwd                                | Repo the agent operates on                                                                                                                |
| `AGENTBRIDGE_HOST`                   | `127.0.0.1`                        | Bind host                                                                                                                                 |
| `AGENTBRIDGE_PORT`                   | `8000`                             | Bind port                                                                                                                                 |
| `AGENTBRIDGE_AGENT`                  | `claude-code`                      | Default agent for new chats                                                                                                               |
| `AGENTBRIDGE_WORKTREE_DIR`           | `<repo>/../.agentbridge-worktrees` | Where branch worktrees are created                                                                                                        |
| `AGENTBRIDGE_STATE_DIR`              | `~/.agentbridge`                   | Where chat history is persisted (per workspace)                                                                                           |
| `AGENTBRIDGE_CLAUDE_SETTING_SOURCES` | `user,project`                     | Which Claude Code settings to load from disk (`local` omitted so no `.claude` files are written into your workspace; empty = CLI default) |
| `GITHUB_TOKEN` / `GH_TOKEN`          | —                                  | Push + PR over HTTPS (falls back to `gh auth token`)                                                                                      |
| `GITHUB_REPOSITORY`                  | —                                  | `owner/name` override when your `origin` remote can't be parsed (e.g. a custom ssh host alias)                                            |

---

## Coding agents

| Agent       | Driver                                 | Status         |
| ----------- | -------------------------------------- | -------------- |
| Claude Code | `claude-agent-sdk` (`ClaudeSDKClient`) | ✅ implemented |
| Cursor      | `cursor-agent` CLI (`stream-json`)     | ✅ implemented |
| Aider       | Python `Coder` API                     | 🚧 stub        |
| Copilot     | `gh copilot` / Copilot CLI             | 🚧 stub        |

Each agent implements the `AgentAdapter` contract in [`backend/agentbridge/agents/base.py`](backend/agentbridge/agents/base.py). Add a new agent by writing one adapter and registering it in `agents/registry.py`.

### Approvals

- **Interactive (Claude Code).** Runs in `default` permission mode: when it wants to edit/write/run a command, the widget shows an **Allow / Deny** card and the agent blocks until you answer.
- **Auto-approve (default on).** A shield toggle lets routine actions run without prompting — but commands that look destructive (`rm -rf`, `sudo`, `git push --force`, `git reset --hard`, `curl … | sh`, recursive `chmod`/`chown`, disk writes, `shutdown`, …) still surface an Allow / Deny card. The preference is per-browser and only affects Claude Code (Cursor runs headless).
- **Stop & queue.** Stop a running turn at any time, and queue follow-up messages while the agent is busy.

### Workspace settings

Agents run _in your workspace_ and honor its own configuration:

- **Claude Code** loads the workspace's `.claude/settings.json`, `.mcp.json`, hooks, custom agents, and `CLAUDE.md`, plus your user settings (`~/.claude`; in Docker, the `agentbridge-claude-home` volume at `/home/app/.claude`). It deliberately omits the `local` source (`.claude/settings.local.json`) so AgentBridge never reads or **creates a `.claude` file in your workspace** — approvals flow through the Allow/Deny card instead. Tune with `AGENTBRIDGE_CLAUDE_SETTING_SOURCES`.
- **Cursor** picks up `.cursor/rules`, `.cursorrules`, and `AGENTS.md` automatically.

---

## How the workflow works

1. Open the bubble → pick an agent → a session starts **on your current branch**.
2. Describe a change → the agent streams its work; edited files show in the footer. Your dev server hot-reloads because edits happen **directly in your workspace**.
3. Click **Create PR** → AgentBridge commits **only the files the agent touched** onto a fresh branch (via a [git worktree](https://git-scm.com/docs/git-worktree), so your checked-out branch is never switched), pushes over HTTPS, and opens a GitHub PR — the link appears in chat.

**Only the agent's changes are committed.** Files you've edited yourself, or other pre-existing changes in the repo, stay in your workspace and are never swept into the PR.

**Meaningful PR titles.** Type a title before clicking Create PR to set it; otherwise AgentBridge derives one (and a body listing the changed files) from the agent's own summary of what it did.

### What the agent sees (page context)

With every message the widget attaches lightweight **browser context**: the current route/URL, detected framework + version, page title, and components on the page. Use the **crosshair (inspect) button** to click any element — its tag, selector, and text are attached, and AgentBridge resolves it to a real file in your repo so the agent goes straight there instead of searching:

- **Route → page file** (e.g. `/auth/login` → `app/auth/login/page.tsx`) for Next.js App/Pages Router, honoring route groups and dynamic segments.
- **Source hint → repo file** from React's dev source info, mapped to a workspace-relative path.
- **Component chain → your component**, walking the React fiber tree to skip library wrappers (e.g. Ant Design's `Wave`) and land on the first component defined in _your_ repo.

### Attaching files

Click the **paperclip** in the composer to attach any files (images, PDFs, data, code) to your next message. They upload over the WebSocket and are saved under a **gitignored** `.agentbridge/uploads/<chat>/` inside your workspace, and the agent is pointed at them by path so it can read them with its normal Read tool (images included). Uploads never show up in git status or get swept into a PR. The per-file size cap is `AGENTBRIDGE_MAX_UPLOAD_MB` (default 25).

### Plugins (MCP servers)

Click the **puzzle-piece** button to connect [MCP](https://modelcontextprotocol.io) servers. Add `stdio` (local command) or `http`/`sse` (remote URL) servers, toggle them on/off, or remove them; the set is persisted per workspace. Enabled servers are passed to Claude Code (alongside any the workspace's own `.mcp.json` already provides), so the agent can use their tools. Changes apply to new or restarted chats. _(MCP wiring currently targets the Claude Code agent.)_

Three one-click **Figma** presets are included:

- **Figma** _(recommended)_ — Figma's hosted server at `https://mcp.figma.com/mcp`. On first use the MCP client opens your browser for Figma's OAuth login — no API key, no desktop app. (The browser opens on the machine running the backend, so this is smoothest when the backend runs **directly on your machine** rather than in Docker.)
- **Figma (Dev Mode)** — the local server from the Figma desktop app (Preferences → _Enable Dev Mode MCP server_), at `http://127.0.0.1:3845/mcp`. ⚠️ If the backend runs in **Docker**, `127.0.0.1` is the container, not your host — change the host to `host.docker.internal:3845` (and run the container with `--add-host=host.docker.internal:host-gateway`).
- **Figma (API key)** — a headless server via `npx figma-developer-mcp` plus a [Figma API token](https://help.figma.com/hc/en-us/articles/8085703771159). No desktop app, no browser — works inside Docker; just replace `YOUR_FIGMA_API_KEY` in the prefilled command.

### Multiple chats, history & resume

One connection runs many chats (**+** to start, **☰** to browse/reopen/delete). Transcripts persist on the backend (`AGENTBRIDGE_STATE_DIR`, per workspace) and survive refreshes and restarts. Reopening a chat **resumes the agent's context** (Claude via its `resume` id, Cursor via `--resume`). Because every chat edits the same workspace, agent turns are **serialized** — one at a time.

### Widget version

The widget is stamped with a build version (package version + git sha), shown next to the title in the header and available as `window.AgentBridge.version` — handy for confirming a deploy is serving the build you expect. The backend serves the bundle with `Cache-Control: no-cache` so a refresh always picks up a rebuild.

---

## GitHub push/PR notes

Pushing and PR creation use a token over **HTTPS** (`GITHUB_TOKEN`/`GH_TOKEN`, or `gh auth token`) — no ssh required, which matters in containers. AgentBridge recognizes both `github.com` remotes and custom ssh host aliases (e.g. `git@github-myorg:owner/repo.git`). If your `origin` can't be parsed, set `GITHUB_REPOSITORY=owner/name` to point it explicitly.

---

## Development

```bash
cd backend && . .venv/bin/activate && pytest    # protocol, git service, registry, session flow
cd widget && node test/smoke.mjs                # jsdom smoke test of the widget bundle
```

## Project layout

```
backend/   FastAPI server, agent adapters, git service, WS protocol, tests
widget/    Shadow-DOM widget (React/assistant-ui thread) + esbuild build + framework examples
```

## Contributing

Issues and PRs welcome. Good first contributions: implementing the Aider/Copilot adapters (one file each behind `AgentAdapter`), or adding framework examples. Please run the backend tests and the widget smoke test before opening a PR.

## License

[MIT](LICENSE) © Omer Spalter
