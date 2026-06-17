# AgentBridge widget

A zero-dependency, framework-agnostic chat bubble that talks to the AgentBridge backend
over WebSocket. It renders entirely inside a **Shadow DOM**, so it never inherits or leaks
CSS to/from the host app.

## Files

- `src/agentbridge-widget.js` — the widget source (the JS is the source of truth)
- `src/agentbridge-widget.css` — styles, injected into the Shadow DOM
- `build.py` — inlines the CSS into the JS to produce `dist/agentbridge-widget.js`
- `examples/` — embed snippets for plain HTML, React, Vue, and Angular

## Build

```bash
python widget/build.py     # writes dist/agentbridge-widget.js (self-contained)
```

The backend serves the built bundle at `/widget/agentbridge-widget.js` and the raw source
at `/widget-src/` (the source build fetches its CSS at runtime).

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
   and the agent waits for your answer (`agent_prompt` → `agent_response`).
4. When the agent first edits a file on the base branch, the widget shows a **"Branch out?"**
   card — accepting it sends `create_branch`. You can also click **Branch** any time. Nothing
   branches automatically. This only *chooses* the branch name: the agent keeps editing in
   the workspace (so your dev server's hot reload shows changes live), and the branch is
   created server-side as a **git worktree** only at PR time.
5. **Create PR** → relocates the in-place edits onto the chosen branch's worktree, commits,
   pushes, and opens a GitHub pull request (your workspace branch stays untouched); the link
   appears in chat.

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
