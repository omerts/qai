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

## Flow

1. Connect → the widget requests the agent list and populates the picker.
2. Pick an agent → starts a session **on the current branch** (no branch is created).
3. Chat → the agent's output streams in; changed files appear in the footer. If the agent
   needs approval (e.g. Claude Code wanting to edit a file), an **Allow / Deny** card appears
   and the agent waits for your answer (`agent_prompt` → `agent_response`).
4. When the agent first edits a file on the base branch, the widget shows a **"Branch out?"**
   card — accepting it sends `create_branch`. You can also click **Branch** any time. Nothing
   branches automatically.
5. **Create PR** → commits, pushes, and opens a GitHub pull request; the link appears in chat.
