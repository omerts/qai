# AgentBridge widget

A framework-agnostic chat bubble that talks to the AgentBridge backend over WebSocket. It
renders entirely inside a **Shadow DOM**, so it never inherits or leaks CSS to/from the host
app. The shell (bubble, controls, inspector, drawer, composer) is plain DOM; the **message
thread** is rendered with [assistant-ui](https://www.assistant-ui.com/) (React) mounted
inside the same Shadow DOM, giving streaming, markdown, and reasoning rendering.

## Files

- `src/agentbridge-widget.js` — the widget shell, WebSocket, controls (entry point)
- `src/thread.jsx` — the assistant-ui React thread + the external-store bridge
- `src/agentbridge-widget.css` — styles, injected into the Shadow DOM
- `build.mjs` — esbuild bundler → `dist/agentbridge-widget.js` (single self-contained IIFE)
- `test/smoke.mjs` — jsdom smoke test (mounts the bundle, streams, checks markdown)
- `examples/` — embed snippets for plain HTML, React, Vue, and Angular

## Build

Prerequisites: **Node 18+** (build toolchain) and the Python backend (to serve the bundle).

```bash
cd widget
npm install            # one-time: React, assistant-ui, esbuild
npm run build          # → dist/agentbridge-widget.js (self-contained IIFE, ~150 KB gzipped)
npm run watch          # rebuild on change (unminified, with sourcemap)
npm test               # jsdom smoke test (mount, stream, markdown, per-agent theming)
```

The bundle in `dist/` is **committed** (the backend and Docker image serve it directly), so
rebuild and commit `dist/` after changing the source. Because the source is now React/JSX
modules, the raw `src/` is no longer directly embeddable — always embed the built bundle.

## Run (see it live)

The widget is hosted by the backend and embedded in a web page:

1. **Build the bundle** (above) so `dist/agentbridge-widget.js` exists.
2. **Start the backend** — it serves the bundle at `/widget/agentbridge-widget.js` and the
   WebSocket at `/ws`. From `backend/`:

   ```bash
   python -m venv .venv && . .venv/bin/activate
   pip install -e ".[claude,dev]"
   AGENTBRIDGE_WORKSPACE=/path/to/your/repo agentbridge   # serves http://127.0.0.1:8000
   ```

   Or, from the repo root, `docker compose up --build`. See the
   [backend README](../backend/README.md) for workspace and auth details.
3. **Embed the script** (see [Embed](#embed) below) on a page served by your app's dev
   server — pointed at the same repo — and the chat bubble appears in the corner.

During UI work, run `npm run watch` in one terminal and the backend in another, then refresh
the host page to pick up rebuilds.

## Embed

One script tag, auto-initialised from `data-*` attributes:

```html
<script
  src="http://localhost:8000/widget/agentbridge-widget.js"
  data-server="ws://localhost:8000/ws"
  data-position="bottom-right"
></script>
```

Or programmatically:

```js
AgentBridge.init({ server: "ws://localhost:8000/ws", position: "bottom-right" });
```

See `examples/` for React (`AgentBridge.jsx`), Vue (`AgentBridge.vue`), and
Angular (`agent-bridge.component.ts`).

## Theming

Override the CSS variables on the widget host if you want to match your brand
(`--ab-accent`, `--ab-bg`, `--ab-fg`, `--ab-radius`, …). They are defined on `:host` in
`src/agentbridge-widget.css`.

## Chats & history

The widget multiplexes several chats over one WebSocket. **+** starts a new chat with the
selected agent; **☰** opens a drawer to switch between, reopen, or delete previous chats.
The selected agent and last-open chat are remembered in `localStorage` (restored on
refresh); transcripts and the agent's resume id are persisted server-side, so reopening a
chat replays its history and continues the agent's context. Each server→client message
carries a `chat_id`; the widget renders only messages for the chat currently on screen.

## Flow

1. Connect → the widget requests the agent list and the chat list, restores your selected
   agent, and reopens your last chat (replaying its transcript).
2. Pick an agent + press **+** → starts a chat **on the current branch** (no branch is created).
3. Chat → the agent's output streams in; changed files appear in the footer. If the agent
   needs approval (e.g. Claude Code wanting to edit a file), an **Allow / Deny** card appears
   and the agent waits for your answer (`agent_prompt` → `agent_response`). The **shield
   toggle** (default on) auto-approves routine edits and safe shell commands so the agent
   runs uninterrupted; risky commands (`rm -rf`, `sudo`, `git push --force`, `curl … | sh`,
   …) still prompt. The toggle is remembered in `localStorage` and sent as `auto_approve` on
   each `user_message`.
4. The agent edits **in place** in the workspace the whole time (so your dev server's hot
   reload shows changes live); nothing branches automatically and your workspace branch is
   never switched.
   - **Stop** — while the agent is working a stop button appears in the composer; it sends
     `{type:"stop"}` and the backend interrupts the current turn (`adapter.interrupt()`).
   - **Queue** — type more while it's busy and your follow-ups queue (shown above the
     composer, removable); each is sent automatically when the agent next goes idle. Stop
     clears the queue.
5. **Create PR** (`create_pr`) → the server derives a branch name, creates a **git worktree**
   for it, relocates the in-place edits onto it, commits, pushes, and opens a GitHub pull
   request (the link appears in chat). It then **resets your workspace** back to a clean
   state on its original branch.

## Page context & element picker

To help the agent understand what the user is looking at, every `user_message` carries a
`context` object the widget collects from the host page:

- **Page**: `url`, `route`, `title`, detected `framework` (React / Vue / Angular + version
  where available), and a best-effort list of `components` on the page.
- **Element** (optional): click the **crosshair button** to enter *inspect mode*, then click
  any element in your app. The widget captures its tag, a CSS selector, text, and — where it
  can resolve them — the owning **component name** (via the framework's runtime: Angular
  `ng.getComponent`, Vue component handles, React fiber walk) and a **source file** hint
  (React dev builds). The selection shows as a removable chip and is attached to your next
  message, then cleared.

All collection is best-effort and wrapped in try/catch — if a framework can't be probed, the
fields are simply omitted. Detection runs only in the page the widget is embedded in.
