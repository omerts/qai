/*
 * AgentBridge widget — a zero-dependency, framework-agnostic chat bubble.
 *
 * Embed with a script tag:
 *   <script src=".../agentbridge-widget.js" data-server="ws://localhost:8000/ws"></script>
 * or programmatically:
 *   AgentBridge.init({ server: "ws://localhost:8000/ws", position: "bottom-right" });
 *
 * Everything renders inside a Shadow DOM, so the host app's CSS cannot leak in and the
 * widget's CSS cannot leak out. This file is the contract's client side — keep message
 * types in sync with backend/agentbridge/protocol.py.
 *
 * One connection multiplexes multiple chats: the widget keeps a chat list, persists the
 * selected agent + active chat in localStorage (so a refresh restores them), and replays a
 * chat's transcript from the server when reopened.
 */
import STYLES from "./agentbridge-widget.css";
import { createThreadBridge, mountThread } from "./thread.jsx";

(function () {
  "use strict";

  // Build-time version stamp (git sha + package version), injected by build.mjs via esbuild
  // `define`. Falls back to "dev" when run unbundled. Exposed below so you can confirm exactly
  // which widget build a page is running (console log, window global, and a data attribute).
  var VERSION = (typeof __AB_WIDGET_VERSION__ !== "undefined") ? __AB_WIDGET_VERSION__ : "dev";

  var ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  // Crosshair "select element" icon (devtools-style inspector).
  var INSPECT_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/><circle cx="12" cy="12" r="4"/></svg>';
  var SHIELD_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z"/><path d="M9 12l2 2 4-4"/></svg>';
  var STOP_ICON = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>';
  // Paperclip "attach file" icon.
  var ATTACH_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a5 5 0 0 1-7.07-7.07l9.19-9.19a3.5 3.5 0 0 1 4.95 4.95l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>';
  // "Update from main" (sync) icon.
  var UPDATE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>';
  // Puzzle-piece "plugins" icon.
  var PLUGIN_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3a2 2 0 0 1 4 0v1h3a1 1 0 0 1 1 1v3h1a2 2 0 0 1 0 4h-1v3a1 1 0 0 1-1 1h-3v1a2 2 0 0 1-4 0v-1H7a1 1 0 0 1-1-1v-3H5a2 2 0 0 1 0-4h1V5a1 1 0 0 1 1-1h3z"/></svg>';

  // Tooltip text for the inspect (crosshair) button, by state.
  var INSPECT_TIP_OFF = "Inspect mode — click, then pick an element on the page to attach it to your next message as context.";
  var INSPECT_TIP_ON = "Inspect mode ON — click an element on the page to attach it (Esc, or click here, to cancel).";

  // Quick-add presets for Figma, prefilled into the form so the user can tweak before saving.
  // 1) Recommended: the hosted remote server. On first use the MCP client opens your browser for
  //    Figma's OAuth login (no key, no desktop app) — the standard flow.
  var FIGMA_PRESET = { name: "figma", transport: "http", url: "https://mcp.figma.com/mcp" };
  // 2) Local Dev Mode MCP server (Figma desktop app → Preferences → "Enable Dev Mode MCP server").
  //    Streamable HTTP at /mcp (the old /sse endpoint is deprecated). Note: if the backend runs in
  //    Docker, 127.0.0.1 is the container — use host.docker.internal:3845 instead.
  var FIGMA_DEVMODE_PRESET = { name: "figma", transport: "http", url: "http://127.0.0.1:3845/mcp" };
  // 3) Headless server via the Framelink package + a Figma API key — works without the desktop app
  //    (and inside Docker, with no browser). Replace YOUR_FIGMA_API_KEY with a token from Figma.
  var FIGMA_KEY_PRESET = {
    name: "figma", transport: "stdio", command: "npx",
    args: ["-y", "figma-developer-mcp", "--figma-api-key=YOUR_FIGMA_API_KEY", "--stdio"],
  };

  var LS_AGENT = "agentbridge:agent";
  var LS_CHAT = "agentbridge:activeChat";
  var LS_AUTO = "agentbridge:autoApprove";
  var LS_MODE = "agentbridge:mode";   // working mode: "default" | "plan"
  var LS_MODEL = "agentbridge:model"; // selected model id (agent-specific; "" = default)
  var LS_EFFORT = "agentbridge:effort"; // selected reasoning-effort id ("" = default)
  var LS_PR_NOTES = "agentbridge:prNotes"; // notes appended to every PR description
  var LS_POS = "agentbridge:pos";   // dragged {left, top} of the widget

  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { window.localStorage.setItem(k, v); } catch (e) {} }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(v, hi)); }

  // Read a File as base64 (no data: prefix) for inline upload over the WebSocket.
  function readFileBase64(file) {
    return new Promise(function (resolve, reject) {
      var r = new FileReader();
      r.onload = function () {
        var res = String(r.result || "");
        var comma = res.indexOf(",");
        resolve(comma >= 0 ? res.slice(comma + 1) : res);
      };
      r.onerror = function () { reject(r.error || new Error("couldn't read file")); };
      r.readAsDataURL(file);
    });
  }

  function h(tag, attrs, children) {
    var el = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      if (k === "class") el.className = attrs[k];
      else if (k === "text") el.textContent = attrs[k];
      else el.setAttribute(k, attrs[k]);
    }
    (children || []).forEach(function (c) {
      el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return el;
  }

  function AgentBridgeWidget(opts) {
    this.server = opts.server;
    this.position = opts.position || "bottom-right";
    this.ws = null;
    this.connected = false;
    this.agents = [];
    this.chats = [];
    this.activeChatId = null;
    this.chatRunning = {};       // chat_id -> bool: which chats are currently working (any chat)
    this.liveChatId = null;      // chat whose changes the dev server is previewing
    this.selectedAgent = lsGet(LS_AGENT) || null;
    this.sessionAgent = null;
    this.branch = null;
    this.targetBranch = null;
    this.bridge = createThreadBridge(); // external store backing the assistant-ui thread
    this.reconnectDelay = 1000;
    this.connState = "connecting"; // connecting | connected | down
    this._everConnected = false;
    this._reconnectTimer = null;
    this.pendingElement = null;  // element picked via the inspector, attached to next msg
    this.attachments = [];       // uploaded files for the next msg: {id, name, size, status, path}
    this._uploadSeq = 0;         // monotonic id source for correlating upload results
    this.mcpServers = [];        // registered MCP plugins (from the server)
    this.skills = [];            // available Agent Skills (for the "/" menu)
    this._slashOpen = false;     // is the "/" skill menu showing
    this._slashItems = [];       // currently-filtered skills
    this._slashIdx = 0;          // highlighted item in the "/" menu
    this.running = false;        // true while the active chat's agent is working
    this.queue = [];             // follow-ups typed while busy: {text, element, attachments}
    this._inspecting = false;
    this.autoApprove = lsGet(LS_AUTO) !== "0";  // default ON; "0" = user turned it off
    this.mode = lsGet(LS_MODE) || "default";    // "default" (code) | "plan"
    this.modelId = lsGet(LS_MODEL) || "";       // selected model id ("" = agent default)
    this.effortId = lsGet(LS_EFFORT) || "";     // selected reasoning effort ("" = agent default)
    this.prNotes = lsGet(LS_PR_NOTES) || "";    // notes appended to every PR description
    this._autoOpened = false;
    this._init();
  }

  AgentBridgeWidget.prototype._init = function () {
    var host = h("div");
    host.style.all = "initial";
    host.setAttribute("data-ab-version", VERSION);   // inspect the DOM to see the running build
    document.body.appendChild(host);
    this.hostEl = host;          // used to exclude our own UI while inspecting
    try { console.log("[AgentBridge] widget " + VERSION); } catch (e) {}
    this.shadow = host.attachShadow({ mode: "open" });

    var style = document.createElement("style");
    style.textContent = STYLES;
    this.shadow.appendChild(style);

    this._buildUI();
    this._mountThread();
    this._connect();
  };

  // Mount the assistant-ui React thread into the messages container (now inside the shadow DOM).
  AgentBridgeWidget.prototype._mountThread = function () {
    this.threadRoot = mountThread(this.messages, this.bridge);
  };

  AgentBridgeWidget.prototype._buildUI = function () {
    var self = this;

    this.root = h("div", { class: "ab-root " + this.position });

    // Launcher bubble (with an offline badge shown when the server is unreachable)
    this.bubble = h("button", { class: "ab-bubble", title: "Ask a coding agent", "aria-label": "Open agent chat" });
    this.bubble.innerHTML = ICON;
    this.bubbleBadge = h("span", { class: "ab-bubble-badge", title: "Agent server offline" });
    this.bubble.appendChild(this.bubbleBadge);
    // Click opens the chat — unless the click is the tail of a drag (then just reposition).
    this.bubble.addEventListener("click", function () {
      if (self._dragJustHappened) { self._dragJustHappened = false; return; }
      self._toggle(true);
    });
    this._enableDrag(this.bubble, { wholeHandle: true, suppressClick: true });

    // Header: menu (chat list) · title · status · new chat · close
    this.statusDot = h("span", { class: "ab-status-dot" });
    this.menuBtn = h("button", { class: "ab-iconbtn", title: "Chats", text: "☰" });
    this.menuBtn.addEventListener("click", function () { self._toggleDrawer(); });
    this.newBtn = h("button", { class: "ab-newbtn ab-tip" });
    this.newBtn.innerHTML = '<span class="ab-newbtn-plus" aria-hidden="true">+</span>New chat';
    this.newBtn.setAttribute("data-tip", "Start a new chat — your current one stays saved in the ☰ list.");
    this.newBtn.setAttribute("aria-label", "New chat");
    this.newBtn.addEventListener("click", function () { self._newChat(); });
    var closeBtn = h("button", { class: "ab-iconbtn", title: "Close", text: "✕" });
    closeBtn.addEventListener("click", function () { self._toggle(false); });
    var titleWrap = h("span", { class: "ab-title", title: "AgentBridge widget " + VERSION }, [
      h("span", { class: "ab-title-name", text: "Coding Agent" }),
      h("span", { class: "ab-ver", text: VERSION }),
    ]);
    var header = h("div", { class: "ab-header ab-drag" }, [
      this.menuBtn,
      this.statusDot,
      titleWrap,
      this.newBtn,
      closeBtn,
    ]);
    this._enableDrag(header);   // drag the panel around by its header

    // Controls: agent picker + PR + inspect
    this.agentSelect = h("select", { class: "ab-select", title: "Default agent for new chats" });
    this.agentSelect.addEventListener("change", function () { self._selectAgent(); });
    // Model (options come from the active agent; shown only when it advertises models).
    this.modelSelect = h("select", { class: "ab-select ab-model", title: "Model" });
    this.modelSelect.addEventListener("change", function () { self._selectModel(); });
    // Reasoning effort (options come from the active agent; shown only when it advertises them).
    this.effortSelect = h("select", { class: "ab-select ab-effort", title: "Reasoning effort" });
    this.effortSelect.addEventListener("change", function () { self._selectEffort(); });
    // Working mode (shown only for agents that support it, e.g. Claude's plan mode).
    this.modeSelect = h("select", { class: "ab-select ab-mode", title: "Working mode" });
    this.modeSelect.appendChild(h("option", { value: "default", text: "Code" }));
    this.modeSelect.appendChild(h("option", { value: "plan", text: "Plan" }));
    this.modeSelect.value = this.mode;
    this.modeSelect.addEventListener("change", function () { self._selectMode(); });
    this.prBtn = h("button", { class: "ab-btn", text: "Create PR", title: "Commit only the agent's edits to a new branch and open a pull request (your other changes stay put). Type a title first to name it, or leave blank to auto-name from the agent's summary." });
    this.prBtn.addEventListener("click", function () { self._createPR(); });
    this.updateBtn = h("button", { class: "ab-iconbtn ab-update ab-tip" });
    this.updateBtn.innerHTML = UPDATE_ICON;
    this.updateBtn.setAttribute("data-tip", "Update this chat from main — the agent resolves any conflicts.");
    this.updateBtn.setAttribute("aria-label", "Update from main");
    this.updateBtn.addEventListener("click", function () { self._updateFromMain(); });
    this.inspectBtn = h("button", { class: "ab-iconbtn ab-inspect ab-tip" });
    this.inspectBtn.innerHTML = INSPECT_ICON;
    this.inspectBtn.setAttribute("data-tip", INSPECT_TIP_OFF);
    this.inspectBtn.setAttribute("aria-label", INSPECT_TIP_OFF);
    this.inspectBtn.addEventListener("click", function () { self._toggleInspect(); });
    this.autoBtn = h("button", { class: "ab-iconbtn ab-autoapprove ab-tip" });
    this.autoBtn.innerHTML = SHIELD_ICON;
    this.autoBtn.addEventListener("click", function () { self._toggleAutoApprove(); });
    this.pluginsBtn = h("button", { class: "ab-iconbtn ab-pluginsbtn ab-tip" });
    this.pluginsBtn.innerHTML = PLUGIN_ICON;
    this.pluginsBtn.setAttribute("data-tip", "Settings — plugins (MCP) and PR description notes.");
    this.pluginsBtn.setAttribute("aria-label", "Settings");
    this.pluginsBtn.addEventListener("click", function () { self._togglePlugins(); });
    this.branchLabel = h("span", { class: "ab-branch-label" });
    var controls = h("div", { class: "ab-controls" }, [
      this.agentSelect, this.modelSelect, this.effortSelect, this.modeSelect, this.prBtn, this.updateBtn, this.inspectBtn, this.autoBtn, this.pluginsBtn, this.branchLabel,
    ]);
    this._refreshAutoApproveBtn();

    // Messages — host node for the assistant-ui React thread (mounted in _mountThread).
    this.messages = h("div", { class: "ab-messages" });

    // Chat-list drawer (overlays the messages area when open)
    this.drawerList = h("div", { class: "ab-drawer-list" });
    this.drawer = h("div", { class: "ab-drawer" }, [
      h("div", { class: "ab-drawer-head", text: "Chats" }),
      this.drawerList,
    ]);

    // Plugins (MCP servers) panel — overlays the messages area when open.
    this._buildPluginsPanel();

    // Changed files
    // Collapsible "changed files" section: a clickable header that toggles the list, which
    // is hidden by default.
    this.filesHead = h("button", { class: "ab-files-head", title: "Show/hide changed files" });
    this.filesHead.addEventListener("click", function () { self._toggleFiles(); });
    this.filesList = h("div", { class: "ab-files-list" });
    this.files = h("div", { class: "ab-files" }, [this.filesHead, this.filesList]);

    // Interactive cards (agent approval prompts, branch suggestions, PR links) dock here,
    // just above the composer — the message list itself is now React/assistant-ui.
    this.cardLayer = h("div", { class: "ab-cards" });

    // Pending attached-element chip
    this.contextBar = h("div", { class: "ab-context-bar" });

    // Queued follow-ups (typed while the agent is busy), shown above the composer.
    this.queueBar = h("div", { class: "ab-queue" });

    // Slash-command menu (skills) shown above the composer when the message starts with "/".
    this.slashMenu = h("div", { class: "ab-slash" });

    // Composer
    this.input = h("textarea", { class: "ab-input", rows: "1", placeholder: "Describe a change… (type / for skills)" });
    this.input.addEventListener("input", function () { self._onComposerInput(); });
    this.input.addEventListener("keydown", function (e) {
      // When the "/" skill menu is open, arrows/enter/tab/esc drive it instead of the composer.
      if (self._slashOpen && self._onSlashKeydown(e)) return;
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); self._sendMessage(); }
    });
    this.stopBtn = h("button", { class: "ab-stop", title: "Stop the agent" });
    this.stopBtn.innerHTML = STOP_ICON;
    this.stopBtn.hidden = true;  // only shown while the agent is working
    this.stopBtn.addEventListener("click", function () { self._stopAgent(); });
    // Attach files: a paperclip that opens a hidden multi-file picker.
    this.attachBtn = h("button", { class: "ab-attach", title: "Attach files for the agent to read" });
    this.attachBtn.innerHTML = ATTACH_ICON;
    this.attachBtn.addEventListener("click", function () { self.fileInput.click(); });
    this.fileInput = h("input", { type: "file", multiple: "", class: "ab-file-input" });
    this.fileInput.addEventListener("change", function () {
      self._uploadFiles(self.fileInput.files);
      self.fileInput.value = "";   // allow re-picking the same file
    });
    this.sendBtn = h("button", { class: "ab-send", text: "➤", title: "Send" });
    this.sendBtn.addEventListener("click", function () { self._sendMessage(); });
    var composer = h("div", { class: "ab-composer" }, [this.attachBtn, this.input, this.stopBtn, this.sendBtn, this.fileInput]);

    // Connection banner (shown when not connected to the server)
    this.bannerText = h("span", { class: "ab-banner-text" });
    this.bannerRetry = h("button", { class: "ab-banner-retry", text: "Retry now" });
    this.bannerRetry.addEventListener("click", function () { self._retryNow(); });
    this.banner = h("div", { class: "ab-banner" }, [
      h("span", { class: "ab-banner-dot" }), this.bannerText, this.bannerRetry,
    ]);

    var body = h("div", { class: "ab-body" }, [this.messages, this.drawer, this.pluginsPanel]);
    var panel = h("div", { class: "ab-panel" }, [header, this.banner, controls, body, this.files, this.cardLayer, this.contextBar, this.queueBar, this.slashMenu, composer]);
    this.root.appendChild(this.bubble);
    this.root.appendChild(panel);
    this.shadow.appendChild(this.root);
    this._restorePosition();   // re-apply a previously dragged position

    this._setConnState("connecting");
    this._setChatActive(false);
  };

  AgentBridgeWidget.prototype._toggle = function (open) {
    // Re-fit the corner anchor before showing the (larger) panel so it can't open off-screen.
    if (open && this._anchor) { this._clampAnchor(); this._applyAnchor(); }
    this.root.classList.toggle("open", open);
    if (open) this.input.focus();
    else if (this._inspecting) this._stopInspect();
  };

  // ---- Dragging --------------------------------------------------------- //

  // Drag the whole widget (panel + bubble share this.root) by a handle, switching from the CSS
  // corner anchor to explicit left/top. Position is clamped to the viewport and remembered.
  // opts.wholeHandle: the element itself is the handle (e.g. the bubble) — don't skip drags that
  // start on it just because it's a button. opts.suppressClick: after an actual drag, swallow the
  // trailing click so a draggable-and-clickable element (the bubble) doesn't also fire its click.
  AgentBridgeWidget.prototype._enableDrag = function (handle, opts) {
    opts = opts || {};
    var self = this;
    this._bindDragGlobals();   // attach the document move/up listeners exactly once

    handle.addEventListener("mousedown", function (e) {
      if (e.button !== 0) return;
      // On a handle that contains its own controls (the header), let those controls work.
      if (!opts.wholeHandle && e.target.closest("button, select, input, textarea, a")) return;
      var r = self.root.getBoundingClientRect();
      self._pinXY(r.left, r.top);
      // Shared per-press state read by the single document-level move/up handlers below.
      self._drag = { opts: opts, moved: false, sx: e.clientX, sy: e.clientY, startLeft: r.left, startTop: r.top };
      document.body.style.userSelect = "none";
      e.preventDefault();
    });
  };

  // Document-level move/up listeners are bound once for the whole widget (not per draggable
  // handle) so additional handles don't accumulate duplicate listeners. They act on the active
  // press recorded in this._drag, which a handle's mousedown sets.
  AgentBridgeWidget.prototype._bindDragGlobals = function () {
    if (this._dragGlobalsBound) return;
    this._dragGlobalsBound = true;
    var self = this;
    var THRESHOLD = 4;   // px of movement before a press counts as a drag (vs a click)

    document.addEventListener("mousemove", function (e) {
      var d = self._drag;
      if (!d) return;
      var dx = e.clientX - d.sx, dy = e.clientY - d.sy;
      if (!d.moved && (Math.abs(dx) > THRESHOLD || Math.abs(dy) > THRESHOLD)) d.moved = true;
      var r = self.root.getBoundingClientRect();
      self._pinXY(
        clamp(d.startLeft + dx, 0, window.innerWidth - r.width),
        clamp(d.startTop + dy, 0, window.innerHeight - r.height)
      );
    }, true);

    document.addEventListener("mouseup", function () {
      var d = self._drag;
      if (!d) return;
      self._drag = null;
      document.body.style.userSelect = "";
      if (!d.moved) return;   // a click, not a drag — leave position and any click handler alone
      if (d.opts.suppressClick) self._dragJustHappened = true;
      // Convert the dragged spot into a corner anchor so the panel opens inward (stays visible).
      self._setAnchorFromRect(self.root.getBoundingClientRect());
    }, true);
  };

  // While dragging, follow the pointer with raw left/top (smooth); converted to a corner anchor
  // on drop. The panel and bubble share this.root, so anchoring by a corner keeps both aligned.
  AgentBridgeWidget.prototype._pinXY = function (left, top) {
    var s = this.root.style;
    s.left = left + "px"; s.top = top + "px"; s.right = "auto"; s.bottom = "auto";
  };

  // Effective panel size, matching the CSS max-width/height clamps.
  AgentBridgeWidget.prototype._panelSize = function () {
    return { w: Math.min(380, window.innerWidth - 32), h: Math.min(560, window.innerHeight - 40) };
  };

  // Derive {h, v, x, y}: which corner to anchor to (nearest), and the distance from those edges,
  // clamped so the full panel fits in the viewport. Applied + persisted.
  AgentBridgeWidget.prototype._setAnchorFromRect = function (r) {
    var hRight = (r.left + r.width / 2) > window.innerWidth / 2;
    var vBottom = (r.top + r.height / 2) > window.innerHeight / 2;
    this._anchor = {
      h: hRight ? "right" : "left",
      v: vBottom ? "bottom" : "top",
      x: hRight ? (window.innerWidth - r.right) : r.left,
      y: vBottom ? (window.innerHeight - r.bottom) : r.top,
    };
    this._clampAnchor();
    this._applyAnchor();
    lsSet(LS_POS, JSON.stringify(this._anchor));
  };

  // Keep the anchored distances small enough that the panel can't open off-screen.
  AgentBridgeWidget.prototype._clampAnchor = function () {
    var a = this._anchor; if (!a) return;
    var ps = this._panelSize();
    a.x = clamp(a.x, 8, Math.max(8, window.innerWidth - ps.w - 8));
    a.y = clamp(a.y, 8, Math.max(8, window.innerHeight - ps.h - 8));
  };

  AgentBridgeWidget.prototype._applyAnchor = function () {
    var a = this._anchor; if (!a) return;
    var s = this.root.style;
    s.left = a.h === "left" ? a.x + "px" : "auto";
    s.right = a.h === "right" ? a.x + "px" : "auto";
    s.top = a.v === "top" ? a.y + "px" : "auto";
    s.bottom = a.v === "bottom" ? a.y + "px" : "auto";
  };

  AgentBridgeWidget.prototype._restorePosition = function () {
    var stored = null, raw = lsGet(LS_POS);
    if (raw) {
      try {
        var a = JSON.parse(raw);
        if (a && (a.h === "left" || a.h === "right") && (a.v === "top" || a.v === "bottom")) stored = a;
      } catch (e) {}
    }
    // Always establish a real anchor — defaulting to the configured corner — and apply it. This
    // makes the initial render and the FIRST open use the exact same positioning as every later
    // open, so the panel never opens off-screen and "auto-corrects" (which also shifted the bubble).
    this._anchor = stored || { h: this.position === "bottom-left" ? "left" : "right", v: "bottom", x: 20, y: 20 };
    this._clampAnchor();   // re-fit in case the viewport changed since the drag
    this._applyAnchor();
  };

  AgentBridgeWidget.prototype._toggleDrawer = function (force) {
    var open = force === undefined ? !this.drawer.classList.contains("open") : force;
    this.drawer.classList.toggle("open", open);
  };
  AgentBridgeWidget.prototype._closeDrawer = function () { this._toggleDrawer(false); };

  // ---- Plugins (MCP servers) ------------------------------------------- //

  AgentBridgeWidget.prototype._buildPluginsPanel = function () {
    var self = this;
    var close = h("button", { class: "ab-iconbtn", title: "Close", text: "✕" });
    close.addEventListener("click", function () { self._togglePlugins(false); });
    var head = h("div", { class: "ab-plugins-head" }, [
      h("span", { class: "ab-plugins-title", text: "Settings" }), close,
    ]);

    // PR notes: free-text appended to every PR description (persisted per-browser).
    this.prNotesInput = h("textarea", { class: "ab-input ab-prnotes", rows: "3",
      placeholder: "Notes appended to every PR description (e.g. ticket ref, reviewer checklist)…" });
    this.prNotesInput.value = this.prNotes || "";
    this.prNotesInput.addEventListener("input", function () {
      self.prNotes = self.prNotesInput.value;
      lsSet(LS_PR_NOTES, self.prNotes);
    });
    var prSection = h("div", { class: "ab-settings-section" }, [
      h("div", { class: "ab-settings-label", text: "PR description notes" }),
      this.prNotesInput,
    ]);

    var intro = h("div", { class: "ab-plugins-intro", text:
      "Plugins — connect MCP servers for the agent to use. Changes apply to new or restarted chats." });
    this.pluginsList = h("div", { class: "ab-plugins-list" });
    // Quick-add presets + a manual "Add server" toggle.
    var figma = h("button", { class: "ab-btn ab-plugins-preset", text: "+ Figma",
      title: "Figma's hosted MCP server — opens your browser to log in on first use (recommended)" });
    figma.addEventListener("click", function () { self._openPluginForm(FIGMA_PRESET); });
    var figmaDev = h("button", { class: "ab-btn ab-plugins-preset", text: "+ Figma (Dev Mode)",
      title: "Local Dev Mode MCP server from the Figma desktop app (http://127.0.0.1:3845/mcp)" });
    figmaDev.addEventListener("click", function () { self._openPluginForm(FIGMA_DEVMODE_PRESET); });
    var figmaKey = h("button", { class: "ab-btn ab-plugins-preset", text: "+ Figma (API key)",
      title: "Headless Figma MCP via npx + a Figma API key — no browser; works in Docker" });
    figmaKey.addEventListener("click", function () { self._openPluginForm(FIGMA_KEY_PRESET); });
    var addBtn = h("button", { class: "ab-btn ab-plugins-add", text: "+ Add server" });
    addBtn.addEventListener("click", function () { self._openPluginForm(null); });
    var presets = h("div", { class: "ab-plugins-presets" }, [figma, figmaDev, figmaKey, addBtn]);
    this.pluginForm = h("div", { class: "ab-plugin-form" });   // populated by _openPluginForm

    // Cursor picks up project rules from the repo itself (not the widget). Note it so users
    // know where to put agent guidance when Cursor is the active agent.
    var cursorNote = h("div", { class: "ab-settings-section ab-cursor-note" }, [
      h("div", { class: "ab-settings-label", text: "Cursor project rules" }),
      h("div", { class: "ab-plugins-intro", text:
        "Cursor reads .cursor/rules, .cursorrules and AGENTS.md from your repo. If only a " +
        "CLAUDE.md is present, its contents are bridged in automatically. Enabled plugins above " +
        "are written to .cursor/mcp.json for Cursor sessions." }),
    ]);

    this.pluginsPanel = h("div", { class: "ab-plugins" }, [head, prSection, intro, this.pluginsList, presets, cursorNote, this.pluginForm]);
  };

  AgentBridgeWidget.prototype._togglePlugins = function (force) {
    var open = force === undefined ? !this.pluginsPanel.classList.contains("open") : force;
    this.pluginsPanel.classList.toggle("open", open);
    if (open) { this._toggleDrawer(false); this._renderPlugins(); }
    else { this.pluginForm.innerHTML = ""; }
  };

  AgentBridgeWidget.prototype._onMcpServers = function (servers) {
    this.mcpServers = servers || [];
    this._renderPlugins();
  };

  AgentBridgeWidget.prototype._renderPlugins = function () {
    if (!this.pluginsPanel.classList.contains("open")) return;
    var self = this;
    this.pluginsList.innerHTML = "";
    if (!this.mcpServers.length) {
      this.pluginsList.appendChild(h("div", { class: "ab-plugins-empty", text: "No plugins yet." }));
      return;
    }
    this.mcpServers.forEach(function (s) {
      var toggle = h("input", { type: "checkbox", class: "ab-plugin-toggle" });
      toggle.checked = s.enabled !== false;
      toggle.addEventListener("change", function () {
        self._send({ type: "toggle_mcp", name: s.name, enabled: toggle.checked });
      });
      var del = h("button", { class: "ab-chip-x", title: "Remove plugin", text: "✕" });
      del.addEventListener("click", function () { self._send({ type: "delete_mcp", name: s.name }); });
      var detail = s.transport === "stdio" ? (s.command || "") : (s.url || "");
      var row = h("div", { class: "ab-plugin-row" }, [
        h("label", { class: "ab-plugin-main" }, [
          toggle,
          h("span", { class: "ab-plugin-name", text: s.name }),
          h("span", { class: "ab-plugin-detail", title: detail, text: s.transport + " · " + detail }),
        ]),
        del,
      ]);
      self.pluginsList.appendChild(row);
    });
  };

  // Render the add/edit form, optionally prefilled from a preset {name, transport, url|command}.
  AgentBridgeWidget.prototype._openPluginForm = function (preset) {
    var self = this;
    preset = preset || { transport: "stdio" };
    this.pluginForm.innerHTML = "";

    var name = h("input", { class: "ab-input ab-plugin-input", placeholder: "name (e.g. figma)" });
    name.value = preset.name || "";
    var transport = h("select", { class: "ab-select ab-plugin-input" });
    ["stdio", "http", "sse"].forEach(function (t) {
      var o = h("option", { value: t, text: t }); if (preset.transport === t) o.selected = true; transport.appendChild(o);
    });
    // stdio fields
    var command = h("input", { class: "ab-input ab-plugin-input", placeholder: "command (e.g. npx)" });
    command.value = preset.command || "";
    var args = h("input", { class: "ab-input ab-plugin-input", placeholder: "args, space-separated" });
    args.value = (preset.args || []).join(" ");
    // remote fields
    var url = h("input", { class: "ab-input ab-plugin-input", placeholder: "https://… or http://127.0.0.1:port/…" });
    url.value = preset.url || "";
    var stdioWrap = h("div", { class: "ab-plugin-fields" }, [command, args]);
    var remoteWrap = h("div", { class: "ab-plugin-fields" }, [url]);

    function sync() {
      var stdio = transport.value === "stdio";
      stdioWrap.style.display = stdio ? "" : "none";
      remoteWrap.style.display = stdio ? "none" : "";
    }
    transport.addEventListener("change", sync);

    var save = h("button", { class: "ab-btn", text: "Save" });
    save.addEventListener("click", function () {
      var spec = { name: name.value.trim(), transport: transport.value, enabled: true };
      if (!spec.name) { self._system("Give the plugin a name."); return; }
      if (transport.value === "stdio") {
        spec.command = command.value.trim();
        spec.args = args.value.trim() ? args.value.trim().split(/\s+/) : [];
      } else {
        spec.url = url.value.trim();
      }
      self._send({ type: "save_mcp", server: spec });
      self.pluginForm.innerHTML = "";
    });
    var cancel = h("button", { class: "ab-btn ab-plugin-cancel", text: "Cancel" });
    cancel.addEventListener("click", function () { self.pluginForm.innerHTML = ""; });

    this.pluginForm.appendChild(h("div", { class: "ab-plugin-form-row" }, [name, transport]));
    this.pluginForm.appendChild(stdioWrap);
    this.pluginForm.appendChild(remoteWrap);
    this.pluginForm.appendChild(h("div", { class: "ab-plugin-form-actions" }, [save, cancel]));
    sync();
  };

  // ---- WebSocket -------------------------------------------------------- //

  AgentBridgeWidget.prototype._connect = function () {
    var self = this;
    if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }
    this._setConnState(this._everConnected ? "down" : "connecting");

    try {
      this.ws = new WebSocket(this.server);
    } catch (e) {
      // Bad URL / blocked: keep retrying with backoff instead of giving up silently.
      this._scheduleReconnect();
      return;
    }
    this.ws.addEventListener("open", function () {
      self.connected = true;
      self._everConnected = true;
      self.reconnectDelay = 1000;
      self._setConnState("connected");
      self._setConnected(true);
      self._autoOpened = false;
      self._send({ type: "list_agents" });
      self._send({ type: "list_chats" });
      self._send({ type: "list_mcp" });
      self._send({ type: "list_skills" });
    });
    this.ws.addEventListener("message", function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      self._onMessage(msg);
    });
    this.ws.addEventListener("close", function () {
      self.connected = false;
      self._setConnState("down");
      self._setConnected(false);
      self._scheduleReconnect();
    });
    this.ws.addEventListener("error", function () { try { self.ws.close(); } catch (e) {} });
  };

  AgentBridgeWidget.prototype._scheduleReconnect = function () {
    var self = this;
    if (this._reconnectTimer) clearTimeout(this._reconnectTimer);
    this._reconnectTimer = setTimeout(function () { self._connect(); }, this.reconnectDelay);
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, 15000);
  };

  // User-triggered "Retry now" — reconnect immediately instead of waiting out the backoff.
  AgentBridgeWidget.prototype._retryNow = function () {
    if (this._reconnectTimer) { clearTimeout(this._reconnectTimer); this._reconnectTimer = null; }
    this.reconnectDelay = 1000;
    try { if (this.ws) this.ws.close(); } catch (e) {}
    this._connect();
  };

  // Reflect connection state in the dot, the banner, the bubble badge, and controls.
  AgentBridgeWidget.prototype._setConnState = function (state) {
    this.connState = state;
    var connected = state === "connected";
    this.statusDot.classList.toggle("connected", connected);
    this.statusDot.classList.toggle("down", state === "down");
    if (!connected) this.statusDot.classList.remove("working");

    this.banner.classList.toggle("show", !connected);
    this.banner.classList.toggle("down", state === "down");
    if (!connected) {
      this.bannerText.textContent = state === "down"
        ? "Can't reach the agent server — reconnecting…"
        : "Connecting to the agent server…";
      // Only offer a manual retry once we've actually failed (not on the first attempt).
      this.bannerRetry.style.display = state === "down" ? "" : "none";
    }

    this.bubble.classList.toggle("offline", !connected);
    this.bubble.title = connected
      ? "Ask a coding agent"
      : "Agent server unavailable — reconnecting";
  };

  AgentBridgeWidget.prototype._send = function (obj) {
    if (this.ws && this.connected) this.ws.send(JSON.stringify(obj));
  };

  // Only render live messages that belong to the chat currently on screen.
  AgentBridgeWidget.prototype._isActive = function (msg) {
    return msg.chat_id && msg.chat_id === this.activeChatId;
  };

  AgentBridgeWidget.prototype._onMessage = function (msg) {
    switch (msg.type) {
      case "agents": return this._onAgents(msg.agents);
      case "chats": return this._onChats(msg.chats);
      case "live_chat": this.liveChatId = msg.chat_id; return this._renderChatList();
      case "skills": this.skills = msg.skills || []; return;
      case "mcp_servers": return this._onMcpServers(msg.servers);
      case "session_started": return this._onSessionStarted(msg);
      case "chat_history": return this._onChatHistory(msg);
      case "chat_deleted": return this._onChatDeleted(msg);
      case "agent_chunk": return this._isActive(msg) && this._onChunk(msg);
      case "agent_prompt": return this._isActive(msg) && this._onPrompt(msg);
      case "branch_created":
        if (!this._isActive(msg)) return;
        this.branch = msg.branch; this.targetBranch = msg.branch; this._updateBranchLabel();
        this._system("Committed your changes to " + msg.branch
          + "\nWorkspace reset to its original branch; worktree: " + msg.worktree_path);
        return;
      case "file_changes": return this._isActive(msg) && this._onFileChanges(msg.files);
      case "file_uploaded": return this._isActive(msg) && this._onFileUploaded(msg);
      case "pr_created": return this._isActive(msg) && this._prLink(msg.url, msg.number);
      case "system_note": return this._isActive(msg) && this.bridge.addSystem(msg.text);
      case "status":
        // Track every chat's running state (so the chat list shows which are working), and drive
        // the active chat's composer/stop UI as before.
        this.chatRunning[msg.chat_id] = (msg.state === "working" || msg.state === "thinking");
        this._renderChatList();
        return this._isActive(msg) && this._onStatus(msg.state);
      case "error":
        if (msg.chat_id && msg.chat_id !== this.activeChatId) return;
        return this.bridge.addSystem(msg.message, "error");
    }
  };

  // ---- Agents + chat list ----------------------------------------------- //

  AgentBridgeWidget.prototype._onAgents = function (agents) {
    var self = this;
    this.agents = agents || [];
    this.agentSelect.innerHTML = "";
    var placeholder = h("option", { value: "", text: "Choose agent…" });
    placeholder.disabled = true;
    this.agentSelect.appendChild(placeholder);
    var restored = false;
    this.agents.forEach(function (a) {
      var opt = h("option", { value: a.name, text: a.label + (a.available ? "" : " (unavailable)") });
      if (!a.available) opt.disabled = true;
      self.agentSelect.appendChild(opt);
      if (a.name === self.selectedAgent && a.available) { opt.selected = true; restored = true; }
    });
    if (!restored) { placeholder.selected = true; this.selectedAgent = null; }
    this._applyTheme();
  };

  AgentBridgeWidget.prototype._onChats = function (chats) {
    this.chats = chats || [];
    this._renderChatList();
    // After a refresh, reopen the chat the user was last in (once per connection).
    if (!this._autoOpened) {
      this._autoOpened = true;
      var want = lsGet(LS_CHAT);
      var exists = want && this.chats.some(function (c) { return c.id === want; });
      if (exists) this._openChat(want);
      else if (!this.activeChatId) this._showEmptyState();
    }
  };

  AgentBridgeWidget.prototype._renderChatList = function () {
    var self = this;
    this.drawerList.innerHTML = "";
    if (!this.chats.length) {
      this.drawerList.appendChild(h("div", { class: "ab-drawer-empty", text: "No chats yet." }));
      return;
    }
    this.chats.forEach(function (c) {
      var running = !!self.chatRunning[c.id];
      var live = c.id === self.liveChatId;
      var item = h("div", { class: "ab-chat-item" + (c.id === self.activeChatId ? " active" : "") });
      var titleRow = h("div", { class: "ab-chat-title-row" }, [
        running ? h("span", { class: "ab-chat-dot", title: "Working…" }) : null,
        h("span", { class: "ab-chat-title", text: c.title || "New chat" }),
      ].filter(Boolean));
      var main = h("div", { class: "ab-chat-main" }, [
        titleRow,
        h("div", { class: "ab-chat-meta", text: c.agent + " · " + c.message_count + " msg" }),
      ]);
      main.addEventListener("click", function () { self._openChat(c.id); });
      // Live toggle: the live chat's changes are mirrored to the workspace (dev server preview).
      var liveBtn = h("button", {
        class: "ab-chat-live" + (live ? " on" : ""),
        title: live ? "Previewing in your dev server — click to stop" : "Preview this chat in your dev server",
        text: live ? "● Live" : "Go live",
      });
      liveBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        self._send({ type: "go_live", chat_id: live ? null : c.id });
      });
      var del = h("button", { class: "ab-chat-del", title: "Delete chat", text: "🗑" });
      del.addEventListener("click", function (e) { e.stopPropagation(); self._deleteChat(c.id); });
      item.appendChild(main);
      item.appendChild(liveBtn);
      item.appendChild(del);
      self.drawerList.appendChild(item);
    });
  };

  // ---- Chat lifecycle --------------------------------------------------- //

  AgentBridgeWidget.prototype._selectAgent = function () {
    this.selectedAgent = this.agentSelect.value || null;
    if (this.selectedAgent) lsSet(LS_AGENT, this.selectedAgent);
    // Preview the picked agent immediately (theme + its pickers).
    this._applyThemeFor(this.selectedAgent);
    if (!this.selectedAgent) return;
    // A chat's agent is fixed at creation, so picking a *different* agent than the open chat
    // (or when nothing is open) starts a fresh chat — otherwise switching the dropdown would
    // silently keep sending your messages to the previous agent.
    if (!this.activeChatId || this.sessionAgent !== this.selectedAgent) this._newChat();
  };

  AgentBridgeWidget.prototype._newChat = function () {
    if (!this.selectedAgent) {
      this._toggleDrawer(false);
      this._system("Pick an agent above, then press + to start a chat.");
      this.agentSelect.focus();
      return;
    }
    this._send({ type: "start_session", agent: this.selectedAgent });
  };

  AgentBridgeWidget.prototype._openChat = function (chatId) {
    if (chatId === this.activeChatId) { this._closeDrawer(); return; }
    this._send({ type: "open_chat", chat_id: chatId });
  };

  AgentBridgeWidget.prototype._deleteChat = function (chatId) {
    this._send({ type: "delete_chat", chat_id: chatId });
  };

  AgentBridgeWidget.prototype._onSessionStarted = function (msg) {
    this.activeChatId = msg.chat_id;
    this.sessionAgent = msg.agent;
    this.branch = msg.branch;
    this.targetBranch = null;
    lsSet(LS_CHAT, msg.chat_id);
    this._resetChatView();
    this._updateBranchLabel();
    this._setChatActive(true);
    this._closeDrawer();
    this._renderChatList();
    this._applyTheme();
  };

  AgentBridgeWidget.prototype._onChatHistory = function (msg) {
    if (msg.chat_id !== this.activeChatId) return;
    var self = this;
    this._resetChatView();
    (msg.entries || []).forEach(function (e) { self._renderEntry(e); });
    if (msg.target_branch) this.targetBranch = msg.target_branch;
    this.branch = msg.target_branch || msg.branch || this.branch;
    this._updateBranchLabel();
    this._onFileChanges(msg.files || []);
  };

  AgentBridgeWidget.prototype._renderEntry = function (e) {
    switch (e.kind) {
      case "user": return this.bridge.addUser(e.text || "");
      case "agent": return this.bridge.addAgent(e.text || "");
      case "system": return this.bridge.addSystem(e.text || "");
      case "branch":
        return this.bridge.addSystem(e.worktree_path
          ? "Committed to " + e.branch + " (" + e.worktree_path + ")"
          : "Target branch: " + e.branch);
      case "pr": return this._prLink(e.url, e.number);
    }
  };

  AgentBridgeWidget.prototype._onChatDeleted = function (msg) {
    if (msg.chat_id === this.activeChatId) {
      this.activeChatId = null;
      this.sessionAgent = null;
      if (lsGet(LS_CHAT) === msg.chat_id) lsSet(LS_CHAT, "");
      this._resetChatView();
      this._setChatActive(false);
      this._showEmptyState();
      this._applyTheme();
    }
  };

  AgentBridgeWidget.prototype._showEmptyState = function () {
    var hint = this.chats.length
      ? "Open a previous chat from ☰, or press + for a new one."
      : "Pick an agent above and press + to start a chat.";
    this.bridge.reset();
    this.bridge.setEmptyHint(hint);
  };

  // ---- Actions ---------------------------------------------------------- //

  AgentBridgeWidget.prototype._sendMessage = function () {
    var text = this.input.value.trim();
    if (!text) return;
    if (!this.activeChatId) { this._newChatHint(); return; }
    if (this.attachments.some(function (a) { return a.status === "uploading"; })) {
      this._system("Still uploading a file — try again in a moment.");
      return;
    }
    this.input.value = "";
    this._hideSlash();
    var element = this.pendingElement || null;
    var attachments = this._readyAttachmentPaths();
    this._clearPendingElement();
    this.attachments = [];
    this._renderContextBar();
    // Busy? Queue the follow-up instead of erroring; it's sent when the turn finishes.
    if (this.running) {
      this.queue.push({ text: text, element: element, attachments: attachments });
      this._renderQueue();
      return;
    }
    this._dispatchUserMessage(text, element, attachments);
  };

  // Workspace-relative paths of attachments that finished uploading (skip in-flight/failed ones).
  AgentBridgeWidget.prototype._readyAttachmentPaths = function () {
    return this.attachments
      .filter(function (a) { return a.status === "ready" && a.path; })
      .map(function (a) { return a.path; });
  };

  // Actually send a user turn to the backend (used for immediate sends and dequeued ones).
  AgentBridgeWidget.prototype._dispatchUserMessage = function (text, element, attachments) {
    this.bridge.addUser(text);
    var context = { page: this._collectPageContext() };
    if (element) context.element = element;
    var payload = {
      type: "user_message", chat_id: this.activeChatId, text: text,
      context: context, auto_approve: this.autoApprove,
    };
    // Only send a non-default mode, and only when the active agent supports modes.
    if (this.mode && this.mode !== "default" && this._activeAgentCaps().plan_mode) {
      payload.mode = this.mode;
    }
    // Send a non-default model/effort only when the active agent advertises them.
    var agent = this._activeAgent();
    if (this.modelId && agent && (agent.models || []).length) {
      payload.model = this.modelId;
    }
    if (this.effortId && agent && (agent.efforts || []).length) {
      payload.effort = this.effortId;
    }
    if (attachments && attachments.length) payload.attachments = attachments;
    this._send(payload);
  };

  AgentBridgeWidget.prototype._stopAgent = function () {
    if (!this.activeChatId || !this.running) return;
    this._send({ type: "stop", chat_id: this.activeChatId });
    this._system("Stopping the agent…");
    // Drop anything queued behind this turn — stopping means "halt", not "run the rest".
    if (this.queue.length) { this.queue = []; this._renderQueue(); }
  };

  // Once the agent goes idle, send the next queued follow-up (one per turn).
  AgentBridgeWidget.prototype._drainQueue = function () {
    if (this.running || !this.queue.length || !this.activeChatId) return;
    var item = this.queue.shift();
    this._renderQueue();
    this._dispatchUserMessage(item.text, item.element, item.attachments);
  };

  AgentBridgeWidget.prototype._renderQueue = function () {
    this.queueBar.innerHTML = "";
    if (!this.queue.length) { this.queueBar.classList.remove("show"); return; }
    this.queueBar.classList.add("show");
    this.queueBar.appendChild(h("div", { class: "ab-queue-head",
      text: this.queue.length + " queued — sent when the agent is free" }));
    var self = this;
    this.queue.forEach(function (item, i) {
      var x = h("button", { class: "ab-queue-x", text: "✕", title: "Remove from queue" });
      x.addEventListener("click", function () { self.queue.splice(i, 1); self._renderQueue(); });
      self.queueBar.appendChild(h("div", { class: "ab-queue-item" }, [
        h("span", { class: "ab-queue-text", text: item.text }), x,
      ]));
    });
  };

  // ---- Slash (/) skill menu -------------------------------------------- //

  // Show the skill menu when the message is just "/<partial>" (no space yet), like Claude Code.
  AgentBridgeWidget.prototype._onComposerInput = function () {
    var m = /^\/(\S*)$/.exec(this.input.value);
    if (!m || !this.skills.length) { this._hideSlash(); return; }
    var q = m[1].toLowerCase();
    this._slashItems = this.skills.filter(function (s) {
      return !q || s.name.toLowerCase().indexOf(q) !== -1;
    });
    if (!this._slashItems.length) { this._hideSlash(); return; }
    this._slashIdx = 0;
    this._slashOpen = true;
    this._renderSlash();
  };

  AgentBridgeWidget.prototype._renderSlash = function () {
    var self = this;
    this.slashMenu.innerHTML = "";
    this.slashMenu.classList.add("show");
    this._slashItems.forEach(function (s, i) {
      var item = h("div", { class: "ab-slash-item" + (i === self._slashIdx ? " sel" : "") }, [
        h("span", { class: "ab-slash-name", text: "/" + s.name }),
        s.description ? h("span", { class: "ab-slash-desc", text: s.description }) : null,
      ].filter(Boolean));
      item.addEventListener("mousedown", function (e) { e.preventDefault(); self._acceptSlash(i); });
      self.slashMenu.appendChild(item);
    });
  };

  AgentBridgeWidget.prototype._hideSlash = function () {
    if (!this._slashOpen && !this.slashMenu.classList.contains("show")) return;
    this._slashOpen = false;
    this._slashItems = [];
    this.slashMenu.classList.remove("show");
    this.slashMenu.innerHTML = "";
  };

  AgentBridgeWidget.prototype._acceptSlash = function (i) {
    var s = this._slashItems[i];
    if (!s) return;
    this.input.value = "/" + s.name + " ";
    this._hideSlash();
    this.input.focus();
  };

  // Returns true if it handled the key (so the composer's own handler is skipped).
  AgentBridgeWidget.prototype._onSlashKeydown = function (e) {
    if (!this._slashOpen || !this._slashItems.length) return false;
    if (e.key === "ArrowDown") {
      e.preventDefault(); this._slashIdx = (this._slashIdx + 1) % this._slashItems.length; this._renderSlash(); return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault(); this._slashIdx = (this._slashIdx - 1 + this._slashItems.length) % this._slashItems.length; this._renderSlash(); return true;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault(); this._acceptSlash(this._slashIdx); return true;
    }
    if (e.key === "Escape") { e.preventDefault(); this._hideSlash(); return true; }
    return false;
  };

  AgentBridgeWidget.prototype._toggleAutoApprove = function () {
    this.autoApprove = !this.autoApprove;
    lsSet(LS_AUTO, this.autoApprove ? "1" : "0");
    this._refreshAutoApproveBtn();
    this._system(this.autoApprove
      ? "Auto-approve on — edits and safe commands run without asking (risky commands still prompt)."
      : "Auto-approve off — you'll confirm each file edit and command.");
  };

  AgentBridgeWidget.prototype._refreshAutoApproveBtn = function () {
    if (!this.autoBtn) return;
    this.autoBtn.classList.toggle("active", this.autoApprove);
    var tip = this.autoApprove
      ? "Auto-approve is ON — edits and safe commands run without asking (risky commands still prompt). Click to require approval."
      : "Auto-approve is OFF — you confirm each file edit and command. Click to let the agent run them automatically.";
    // Drive the custom .ab-tip tooltip (data-tip) and keep aria-label for screen readers;
    // no native `title` so the two tooltips don't show at once.
    this.autoBtn.setAttribute("data-tip", tip);
    this.autoBtn.setAttribute("aria-label", tip);
  };

  AgentBridgeWidget.prototype._newChatHint = function () {
    this._system("No chat open — pick an agent and press + to start one.");
  };

  AgentBridgeWidget.prototype._createPR = function () {
    if (!this.activeChatId) return this._newChatHint();
    // A typed title wins; otherwise let the backend name the PR from the agent's own summary.
    var typed = this.input.value.trim();
    this.input.value = "";
    var msg = { type: "create_pr", chat_id: this.activeChatId };
    if (typed) msg.title = typed;
    if (this.prNotes && this.prNotes.trim()) msg.notes = this.prNotes;   // configured PR notes
    this._send(msg);
    this._system(typed ? "Creating pull request…" : "Summarizing changes and creating pull request…");
  };

  AgentBridgeWidget.prototype._updateFromMain = function () {
    if (!this.activeChatId) return this._newChatHint();
    if (this.running) { this._system("Wait for the agent to finish, then update from main."); return; }
    this._send({ type: "update_branch", chat_id: this.activeChatId });
    this._system("Updating from main…");
  };

  // ---- Page context collection ----------------------------------------- //

  AgentBridgeWidget.prototype._collectPageContext = function () {
    var ctx = { url: "", route: "", title: "" };
    try {
      ctx.url = location.href;
      ctx.route = location.pathname + location.search + location.hash;
      ctx.title = document.title;
      ctx.framework = this._detectFramework();
      ctx.components = this._collectComponents();
    } catch (e) { /* best-effort */ }
    return ctx;
  };

  AgentBridgeWidget.prototype._detectFramework = function () {
    try {
      var ng = document.querySelector("[ng-version]");
      if (ng) return { name: "Angular", version: ng.getAttribute("ng-version") };
    } catch (e) {}
    try {
      if (window.Vue && window.Vue.version) return { name: "Vue", version: window.Vue.version };
      if (document.querySelector("[data-v-app]") || window.__VUE__) return { name: "Vue", version: null };
    } catch (e) {}
    try {
      if (window.React && window.React.version) return { name: "React", version: window.React.version };
      if (this._reactRootPresent()) return { name: "React", version: null };
    } catch (e) {}
    return { name: null, version: null };
  };

  AgentBridgeWidget.prototype._reactRootPresent = function () {
    var roots = document.querySelectorAll("#root, #app, body > div");
    for (var i = 0; i < roots.length && i < 10; i++) {
      var el = roots[i];
      if (el._reactRootContainer) return true;
      for (var k in el) {
        if (k.indexOf("__reactContainer$") === 0 || k.indexOf("__reactFiber$") === 0) return true;
      }
    }
    return false;
  };

  AgentBridgeWidget.prototype._collectComponents = function () {
    var seen = {}, out = [];
    function add(n) { if (n && !seen[n]) { seen[n] = 1; out.push(n); } }
    try {
      var all = document.body ? document.body.getElementsByTagName("*") : [];
      for (var i = 0; i < all.length && i < 4000 && out.length < 40; i++) {
        var tag = all[i].tagName.toLowerCase();
        if (tag.indexOf("-") !== -1) add(tag);
      }
    } catch (e) {}
    try {
      if (window.ng && typeof window.ng.getComponent === "function") {
        var ngEls = document.querySelectorAll("[ng-version] *");
        for (var j = 0; j < ngEls.length && j < 500 && out.length < 40; j++) {
          var c = window.ng.getComponent(ngEls[j]);
          if (c && c.constructor) add(c.constructor.name);
        }
      }
    } catch (e) {}
    return out.slice(0, 40);
  };

  // ---- Element inspector (devtools-style "select element") ------------- //

  AgentBridgeWidget.prototype._toggleInspect = function () {
    if (this._inspecting) { this._stopInspect(); return; }
    this._inspecting = true;
    this.inspectBtn.classList.add("active");
    this.inspectBtn.setAttribute("data-tip", INSPECT_TIP_ON);
    var self = this;

    var ov = document.createElement("div");
    ov.style.cssText = "position:fixed;pointer-events:none;z-index:2147483646;display:none;"
      + "background:rgba(79,70,229,.18);border:1px solid #4f46e5;border-radius:2px;";
    document.body.appendChild(ov);
    this._overlay = ov;

    this._onMove = function (e) { self._inspectMove(e); };
    this._onClick = function (e) { self._inspectClick(e); };
    this._onKey = function (e) { if (e.key === "Escape") self._stopInspect(); };
    document.addEventListener("mousemove", this._onMove, true);
    document.addEventListener("click", this._onClick, true);
    document.addEventListener("keydown", this._onKey, true);
    this._system("Inspect mode on — click an element on the page to attach it (Esc to cancel).");
  };

  // `attached` is true when stopping because an element was just picked — that has its own
  // feedback (the context chip), so we skip the "off" note in that case.
  AgentBridgeWidget.prototype._stopInspect = function (attached) {
    if (!this._inspecting) return;
    this._inspecting = false;
    if (this.inspectBtn) {
      this.inspectBtn.classList.remove("active");
      this.inspectBtn.setAttribute("data-tip", INSPECT_TIP_OFF);
    }
    if (this._overlay) { this._overlay.remove(); this._overlay = null; }
    document.removeEventListener("mousemove", this._onMove, true);
    document.removeEventListener("click", this._onClick, true);
    document.removeEventListener("keydown", this._onKey, true);
    if (!attached) this._system("Inspect mode off.");
  };

  AgentBridgeWidget.prototype._inspectTarget = function (e) {
    var path = e.composedPath ? e.composedPath() : [];
    if (path.indexOf(this.hostEl) !== -1) return null;
    return e.target && e.target.nodeType === 1 ? e.target : null;
  };

  AgentBridgeWidget.prototype._inspectMove = function (e) {
    var el = this._inspectTarget(e);
    if (!el) { this._overlay.style.display = "none"; return; }
    var r = el.getBoundingClientRect();
    var ov = this._overlay;
    ov.style.display = "block";
    ov.style.left = r.left + "px";
    ov.style.top = r.top + "px";
    ov.style.width = r.width + "px";
    ov.style.height = r.height + "px";
  };

  AgentBridgeWidget.prototype._inspectClick = function (e) {
    var el = this._inspectTarget(e);
    if (!el) return; // click landed on our widget — let it behave normally
    e.preventDefault();
    e.stopPropagation();
    this._setPendingElement(this._describeElement(el));
    this._stopInspect(true);
  };

  AgentBridgeWidget.prototype._describeElement = function (el) {
    var tag = el.tagName.toLowerCase();
    var idPart = el.id ? "#" + el.id : "";
    var clsPart = "";
    if (typeof el.className === "string" && el.className.trim()) {
      clsPart = "." + el.className.trim().split(/\s+/).slice(0, 3).join(".");
    }
    var attrs = {};
    ["role", "aria-label", "name", "type", "href", "data-testid"].forEach(function (a) {
      var v = el.getAttribute && el.getAttribute(a);
      if (v) attrs[a] = v;
    });
    return {
      label: "<" + tag + idPart + clsPart + ">",
      tag: tag,
      id: el.id || null,
      text: (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 160),
      selector: this._cssPath(el),
      component: this._resolveComponent(el),
      // The full innermost->outermost chain of component names. The nearest one is often a
      // library internal (e.g. Ant Design's "Wave"); the backend walks this to find the first
      // name that maps to a file in the user's repo.
      componentChain: this._componentChain(el),
      source: this._sourceHint(el),
      attributes: attrs
    };
  };

  AgentBridgeWidget.prototype._componentChain = function (el) {
    var names = [];
    try {
      var key = Object.keys(el).find(function (k) {
        return k.indexOf("__reactFiber$") === 0 || k.indexOf("__reactInternalInstance$") === 0;
      });
      if (!key) return names;
      var seen = {};
      for (var fiber = el[key], d = 0; fiber && d < 60; d++, fiber = fiber.return) {
        var t = fiber.type, nm = null;
        if (typeof t === "function") nm = t.displayName || t.name;
        else if (t && typeof t === "object") nm = t.displayName || (t.render && (t.render.displayName || t.render.name));
        // Components are PascalCase; skip lowercase host tags and dups.
        if (nm && nm[0] === nm[0].toUpperCase() && !seen[nm]) { seen[nm] = 1; names.push(nm); }
      }
    } catch (e) {}
    // Keep a generous slice: the user's component can sit deep behind library wrappers (an Ant
    // Design button buries LoginPage ~15 levels up), and the backend filters out the noise.
    return names.slice(0, 40);
  };

  AgentBridgeWidget.prototype._cssPath = function (el) {
    if (!el || el.nodeType !== 1) return "";
    var parts = [];
    while (el && el.nodeType === 1 && el !== document.body && parts.length < 6) {
      var sel = el.nodeName.toLowerCase();
      if (el.id) { parts.unshift(sel + "#" + el.id); break; }
      var parent = el.parentNode;
      if (parent) {
        var sibs = Array.prototype.filter.call(parent.children, function (c) {
          return c.nodeName === el.nodeName;
        });
        if (sibs.length > 1) sel += ":nth-of-type(" + (Array.prototype.indexOf.call(sibs, el) + 1) + ")";
      }
      parts.unshift(sel);
      el = el.parentNode;
    }
    return parts.join(" > ");
  };

  AgentBridgeWidget.prototype._resolveComponent = function (el) {
    var node;
    try {
      if (window.ng && typeof window.ng.getComponent === "function") {
        for (node = el; node; node = node.parentElement) {
          var c = window.ng.getComponent(node);
          if (c && c.constructor && c.constructor.name) return c.constructor.name;
        }
      }
    } catch (e) {}
    try { // Vue 3 / Vue 2
      for (node = el; node; node = node.parentElement) {
        var vc = node.__vueParentComponent;
        if (vc && vc.type) { var nm = vc.type.__name || vc.type.name; if (nm) return nm; }
        if (node.__vue__ && node.__vue__.$options && node.__vue__.$options.name) return node.__vue__.$options.name;
      }
    } catch (e) {}
    try { // React fiber walk
      var key = Object.keys(el).find(function (k) {
        return k.indexOf("__reactFiber$") === 0 || k.indexOf("__reactInternalInstance$") === 0;
      });
      if (key) {
        var fiber = el[key];
        for (var d = 0; fiber && d < 40; d++, fiber = fiber.return) {
          var t = fiber.type;
          if (typeof t === "function") {
            var rn = t.displayName || t.name;
            if (rn && rn[0] === rn[0].toUpperCase()) return rn;
          } else if (t && typeof t === "object") {
            if (t.displayName) return t.displayName;
            if (t.render && (t.render.displayName || t.render.name)) return t.render.displayName || t.render.name;
          }
        }
      }
    } catch (e) {}
    return null;
  };

  AgentBridgeWidget.prototype._sourceHint = function (el) {
    // 1) Some dev tooling (e.g. react-dev-inspector, vite plugins) stamps the source straight
    //    onto the DOM. Cheapest and most reliable when present.
    try {
      for (var node = el; node && node.nodeType === 1; node = node.parentElement) {
        var rel = node.getAttribute("data-inspector-relative-path") || node.getAttribute("data-source-file");
        if (rel) {
          return {
            file: rel,
            line: parseInt(node.getAttribute("data-inspector-line") || node.getAttribute("data-source-line"), 10) || null,
            column: parseInt(node.getAttribute("data-inspector-column") || "", 10) || null,
          };
        }
      }
    } catch (e) {}
    // 2) React fiber: _debugSource (React ≤18) or the __source prop the JSX-source Babel plugin
    //    attaches (both dev-only). Walk up, preferring app source over library frames so the
    //    hint points at the user's component, not a node_modules wrapper.
    try {
      var key = Object.keys(el).find(function (k) { return k.indexOf("__reactFiber$") === 0; });
      if (!key) return null;
      var fiber = el[key];
      var fallback = null;
      for (var d = 0; fiber && d < 60; d++, fiber = fiber.return) {
        var src = fiber._debugSource
          || (fiber.memoizedProps && fiber.memoizedProps.__source)
          || (fiber.pendingProps && fiber.pendingProps.__source);
        if (src && src.fileName) {
          var hint = { file: src.fileName, line: src.lineNumber || null, column: src.columnNumber || null };
          if (src.fileName.indexOf("node_modules") === -1) return hint; // app source — best match
          if (!fallback) fallback = hint;                                // library frame — keep looking
        }
      }
      return fallback;
    } catch (e) {}
    return null;
  };

  // ---- Context bar: pending element + attachment chips ------------------ //

  AgentBridgeWidget.prototype._setPendingElement = function (desc) {
    this.pendingElement = desc;
    this._renderContextBar();
  };

  AgentBridgeWidget.prototype._clearPendingElement = function () {
    this.pendingElement = null;
    this._renderContextBar();
  };

  // Render the element chip (if any) plus a chip per pending attachment. Shown only when there's
  // something to show.
  AgentBridgeWidget.prototype._renderContextBar = function () {
    var self = this;
    this.contextBar.innerHTML = "";
    var any = false;

    var el = this.pendingElement;
    if (el) {
      any = true;
      var label = el.component ? "‹" + el.component + "› " + el.label : el.label;
      var elChip = h("span", { class: "ab-chip" }, [
        h("span", { class: "ab-chip-icon", text: "🎯" }),
        h("span", { class: "ab-chip-text", title: el.selector, text: label }),
      ]);
      var elX = h("button", { class: "ab-chip-x", title: "Remove", text: "✕" });
      elX.addEventListener("click", function () { self._clearPendingElement(); });
      elChip.appendChild(elX);
      this.contextBar.appendChild(elChip);
    }

    this.attachments.forEach(function (att) {
      any = true;
      var icon = att.status === "uploading" ? "⏳" : att.status === "error" ? "⚠️" : "📎";
      var cls = "ab-chip ab-chip-file" + (att.status === "error" ? " error" : "");
      var chip = h("span", { class: cls }, [
        h("span", { class: "ab-chip-icon", text: icon }),
        h("span", { class: "ab-chip-text", title: att.error || att.path || att.name, text: att.name }),
      ]);
      var x = h("button", { class: "ab-chip-x", title: "Remove", text: "✕" });
      x.addEventListener("click", function () { self._removeAttachment(att.id); });
      chip.appendChild(x);
      self.contextBar.appendChild(chip);
    });

    this.contextBar.classList.toggle("show", any);
  };

  AgentBridgeWidget.prototype._removeAttachment = function (id) {
    this.attachments = this.attachments.filter(function (a) { return a.id !== id; });
    this._renderContextBar();
  };

  // Read each picked file as base64 and upload it; the result (path) comes back via file_uploaded.
  AgentBridgeWidget.prototype._uploadFiles = function (files) {
    if (!files || !files.length) return;
    if (!this.activeChatId) { this._newChatHint(); return; }
    if (!this.connected) { this._system("Can't upload while disconnected from the agent server."); return; }
    var self = this;
    Array.prototype.forEach.call(files, function (file) {
      var id = "u" + (++self._uploadSeq);
      var att = { id: id, name: file.name || "file", size: file.size, status: "uploading", path: null };
      self.attachments.push(att);
      self._renderContextBar();
      readFileBase64(file).then(function (b64) {
        // The chip may have been removed while reading; only send if it's still pending.
        if (self.attachments.indexOf(att) === -1) return;
        self._send({ type: "upload_file", chat_id: self.activeChatId, upload_id: id, name: att.name, data: b64 });
      }).catch(function (e) {
        att.status = "error"; att.error = (e && e.message) || "couldn't read file";
        self._renderContextBar();
      });
    });
  };

  AgentBridgeWidget.prototype._onFileUploaded = function (msg) {
    var att = null;
    for (var i = 0; i < this.attachments.length; i++) {
      if (this.attachments[i].id === msg.upload_id) { att = this.attachments[i]; break; }
    }
    if (!att) return;   // chip was removed before the result arrived
    if (msg.ok) {
      att.status = "ready"; att.path = msg.path;
      if (msg.name) att.name = msg.name;
      if (msg.size != null) att.size = msg.size;
    } else {
      att.status = "error"; att.error = msg.error || "upload failed";
    }
    this._renderContextBar();
  };

  // ---- Rendering -------------------------------------------------------- //

  AgentBridgeWidget.prototype._onChunk = function (msg) {
    this.bridge.chunk(msg.text, msg.stream);
  };

  AgentBridgeWidget.prototype._onPrompt = function (msg) {
    var self = this;
    function reply(answer) {
      self._send({ type: "agent_response", chat_id: msg.chat_id, request_id: msg.request_id, answer: answer });
      card.remove();
    }

    if (msg.options && msg.options.length && msg.multi) {
      // Multi-select: a checkbox per option + a Submit button; answer is the picks, comma-joined.
      var rows = msg.options.map(function (opt) {
        var cb = h("input", { type: "checkbox", class: "ab-check" });
        return { opt: opt, cb: cb, row: h("label", { class: "ab-check-row" }, [cb, h("span", { text: opt })]) };
      });
      var submit = h("button", { class: "ab-btn primary", text: "Submit" });
      submit.addEventListener("click", function () {
        var picked = rows.filter(function (r) { return r.cb.checked; }).map(function (r) { return r.opt; });
        reply(picked.length ? picked.join(", ") : "(none selected)");
      });
      // Assign the shared `card` (the reply() closure removes it on submit).
      card = this._card(msg.title || "Agent needs your input", msg.prompt, [submit]);
      var mactions = card.querySelector(".ab-card-actions");
      rows.forEach(function (r) { card.insertBefore(r.row, mactions); });
      return;
    }

    if (msg.options && msg.options.length) {
      var buttons = msg.options.map(function (opt, i) {
        var b = h("button", { class: "ab-btn" + (i === 0 ? " primary" : ""), text: opt });
        b.addEventListener("click", function () { reply(opt); });
        return b;
      });
      var card = this._card(msg.title || "Agent needs your approval", msg.prompt, buttons);
      return;
    }

    var input = h("input", { class: "ab-prompt-input ab-select", type: "text", placeholder: "Your answer…" });
    var send = h("button", { class: "ab-btn primary", text: "Reply" });
    send.addEventListener("click", function () { reply(input.value); });
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") reply(input.value); });
    var card = this._card(msg.title || "Agent needs input", msg.prompt, [send]);
    card.insertBefore(input, card.querySelector(".ab-card-actions"));
    input.focus();
  };

  AgentBridgeWidget.prototype._onFileChanges = function (files) {
    this.filesList.innerHTML = "";
    if (!files || !files.length) { this.files.classList.remove("show"); return; }
    this._renderFilesHead(files.length);
    var self = this;
    files.forEach(function (f) {
      self.filesList.appendChild(h("div", { class: "ab-file" }, [
        h("span", { class: "code", text: f.status }),
        h("span", { class: "ab-file-path", text: f.path }),
      ]));
    });
    this.files.classList.add("show");  // header visible; list stays collapsed until toggled
  };

  AgentBridgeWidget.prototype._renderFilesHead = function (n) {
    var open = this.files.classList.contains("open");
    this.filesHead.innerHTML = '<span class="ab-files-caret" aria-hidden="true">▸</span>'
      + '<span>' + n + ' changed file' + (n > 1 ? 's' : '') + '</span>';
    this.filesHead.setAttribute("aria-expanded", open ? "true" : "false");
  };

  AgentBridgeWidget.prototype._toggleFiles = function () {
    var open = this.files.classList.toggle("open");
    this.filesHead.setAttribute("aria-expanded", open ? "true" : "false");
  };

  AgentBridgeWidget.prototype._onStatus = function (state) {
    var working = state === "working";
    this.running = working;
    this.statusDot.classList.toggle("working", working);
    this.stopBtn.hidden = !working;  // Stop is available only while the agent is working
    // Send stays enabled while working so follow-ups can be queued.
    this.input.placeholder = working ? "Queue a follow-up… (Enter)" : "Describe a change…";
    this.bridge.setRunning(working, this._agentLabel(this._activeAgentName()));
    if (!working) {
      this.bridge.clearThinking();  // turn done — drop transient progress, keep the answer
      this._drainQueue();
    }
  };

  AgentBridgeWidget.prototype._agentLabel = function (name) {
    for (var i = 0; i < this.agents.length; i++) {
      if (this.agents[i].name === name) return this.agents[i].label || name;
    }
    return name || "Assistant";
  };

  AgentBridgeWidget.prototype._prLink = function (url, number) {
    var link = h("a", { href: url, target: "_blank", text: "View PR" + (number ? " #" + number : "") });
    link.style.color = "var(--ab-accent)";
    var card = this._card("Pull request created", null, []);
    card.querySelector(".ab-card-body").appendChild(link);
  };

  // ---- DOM helpers ------------------------------------------------------ //

  // A system note in the thread (rendered by assistant-ui as a system message).
  AgentBridgeWidget.prototype._system = function (text) { this.bridge.addSystem(text); };

  // Interactive cards still live in the (vanilla) card layer above the composer.
  AgentBridgeWidget.prototype._card = function (title, bodyText, actions) {
    var card = h("div", { class: "ab-card" }, [
      h("div", { class: "ab-card-title", text: title }),
      h("div", { class: "ab-card-body", text: bodyText || "" }),
    ]);
    var actionsRow = h("div", { class: "ab-card-actions" }, actions || []);
    card.appendChild(actionsRow);
    this.cardLayer.appendChild(card);
    return card;
  };

  AgentBridgeWidget.prototype._resetChatView = function () {
    this.bridge.reset();
    this.cardLayer.innerHTML = "";
    this.filesList.innerHTML = "";
    this.files.classList.remove("show", "open");
    this.queue = [];
    this._renderQueue();
    this.running = false;
    if (this.stopBtn) this.stopBtn.hidden = true;
    this._clearPendingElement();
  };

  // ---- Per-agent theming ------------------------------------------------ //
  // The active agent's accent (sent by the server in AgentInfo.theme) is set as inline CSS
  // variables on the widget root, overriding the :host defaults. These cascade into the
  // assistant-ui React thread too (its bubbles/links use var(--ab-accent)), so the whole
  // widget — shell and thread — recolors to the selected agent.

  AgentBridgeWidget.prototype._agentTheme = function (name) {
    for (var i = 0; i < this.agents.length; i++) {
      if (this.agents[i].name === name) return this.agents[i].theme || null;
    }
    return null;
  };

  // The agent whose theme should show: the open chat's agent (new or reopened) wins;
  // otherwise the agent selected in the picker.
  AgentBridgeWidget.prototype._activeAgentName = function () {
    if (this.sessionAgent) return this.sessionAgent;
    if (this.activeChatId) {
      for (var i = 0; i < this.chats.length; i++) {
        if (this.chats[i].id === this.activeChatId) return this.chats[i].agent;
      }
    }
    return this.selectedAgent;
  };

  AgentBridgeWidget.prototype._applyTheme = function () {
    this._applyThemeFor(this._activeAgentName());
  };

  AgentBridgeWidget.prototype._agentByName = function (name) {
    for (var i = 0; i < this.agents.length; i++) {
      if (this.agents[i].name === name) return this.agents[i];
    }
    return null;
  };

  // Apply a specific agent's accent theme + refresh the per-agent pickers (model/effort/mode) to
  // that agent's capabilities.
  AgentBridgeWidget.prototype._applyThemeFor = function (name) {
    if (!this.root) return;
    var agent = this._agentByName(name);
    var theme = (agent && agent.theme) || null;
    var vars = { accent: "--ab-accent", accentFg: "--ab-accent-fg" };
    for (var key in vars) {
      if (theme && theme[key]) this.root.style.setProperty(vars[key], theme[key]);
      else this.root.style.removeProperty(vars[key]);
    }
    this._updateModeVisibility(agent);
    this._updateModelOptions(agent);
    this._updateEffortOptions(agent);
  };

  // The agent record currently in effect, or null.
  AgentBridgeWidget.prototype._activeAgent = function () {
    var name = this._activeAgentName();
    for (var i = 0; i < this.agents.length; i++) {
      if (this.agents[i].name === name) return this.agents[i];
    }
    return null;
  };

  // Capabilities of the agent currently in effect, or {} if unknown.
  AgentBridgeWidget.prototype._activeAgentCaps = function () {
    var a = this._activeAgent();
    return (a && a.capabilities) || {};
  };

  // Only show the mode picker for agents that support modes (e.g. Claude's plan mode).
  AgentBridgeWidget.prototype._updateModeVisibility = function (agent) {
    if (!this.modeSelect) return;
    agent = agent || this._activeAgent();
    this.modeSelect.hidden = !((agent && agent.capabilities) || {}).plan_mode;
  };

  // Fill a <select> from agent-advertised options [{id,label}]; hide it if none. Returns the
  // resolved current id — kept if the agent still offers it, else the first (default).
  AgentBridgeWidget.prototype._fillAgentSelect = function (select, options, current) {
    options = options || [];
    select.hidden = !options.length;
    if (!options.length) return current;
    select.innerHTML = "";
    var ids = [];
    options.forEach(function (o) {
      ids.push(o.id);
      select.appendChild(h("option", { value: o.id, text: o.label }));
    });
    if (ids.indexOf(current) === -1) current = ids[0] || "";
    select.value = current;
    return current;
  };

  AgentBridgeWidget.prototype._updateModelOptions = function (a) {
    if (!this.modelSelect) return;
    a = a || this._activeAgent();
    this.modelId = this._fillAgentSelect(this.modelSelect, a && a.models, this.modelId);
  };

  AgentBridgeWidget.prototype._updateEffortOptions = function (a) {
    if (!this.effortSelect) return;
    a = a || this._activeAgent();
    this.effortId = this._fillAgentSelect(this.effortSelect, a && a.efforts, this.effortId);
  };

  AgentBridgeWidget.prototype._selectModel = function () {
    this.modelId = this.modelSelect.value || "";
    lsSet(LS_MODEL, this.modelId);
    var label = this.modelSelect.options[this.modelSelect.selectedIndex];
    this._system("Model set to " + ((label && label.text) || "default") + " for new messages.");
  };

  AgentBridgeWidget.prototype._selectEffort = function () {
    this.effortId = this.effortSelect.value || "";
    lsSet(LS_EFFORT, this.effortId);
    var label = this.effortSelect.options[this.effortSelect.selectedIndex];
    this._system("Reasoning effort set to " + ((label && label.text) || "default") + " for new messages.");
  };

  AgentBridgeWidget.prototype._selectMode = function () {
    this.mode = this.modeSelect.value || "default";
    lsSet(LS_MODE, this.mode);
    this._system(this.mode === "plan"
      ? "Plan mode — the agent will analyze and propose a plan without making changes. Approve the plan to let it proceed."
      : "Code mode — the agent makes changes directly.");
  };

  AgentBridgeWidget.prototype._updateBranchLabel = function () {
    var b = this.targetBranch || this.branch;
    this.branchLabel.textContent = b ? "⛓ " + b : "";
  };

  AgentBridgeWidget.prototype._setConnected = function (on) {
    this.menuBtn.disabled = !on;
    this.newBtn.disabled = !on;
    this.agentSelect.disabled = !on;
    this.pluginsBtn.disabled = !on;   // plugins are workspace-level but need the server
    if (!on) { this._setChatActive(false); this._togglePlugins(false); }
  };

  AgentBridgeWidget.prototype._setChatActive = function (on) {
    this.prBtn.disabled = !on;
    this.updateBtn.disabled = !on;
    this.inspectBtn.disabled = !on;
    this.attachBtn.disabled = !on;
    this.sendBtn.disabled = !on;
    this.input.disabled = !on;
  };

  // ---- Public API + auto-init ------------------------------------------ //

  var AgentBridge = {
    version: VERSION,   // read window.AgentBridge.version in the console to check the build
    _instance: null,
    init: function (opts) {
      opts = opts || {};
      if (!opts.server) { console.error("[AgentBridge] init requires a `server` URL"); return null; }
      this._instance = new AgentBridgeWidget(opts);
      return this._instance;
    },
  };

  var current = document.currentScript;
  if (current && current.dataset && current.dataset.server) {
    AgentBridge.init({
      server: current.dataset.server,
      position: current.dataset.position || "bottom-right",
    });
  }

  window.AgentBridge = AgentBridge;
})();
