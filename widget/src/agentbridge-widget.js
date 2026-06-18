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

  var ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  // Crosshair "select element" icon (devtools-style inspector).
  var INSPECT_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/><circle cx="12" cy="12" r="4"/></svg>';
  var SHIELD_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z"/><path d="M9 12l2 2 4-4"/></svg>';

  // Tooltip text for the inspect (crosshair) button, by state.
  var INSPECT_TIP_OFF = "Inspect mode — click, then pick an element on the page to attach it to your next message as context.";
  var INSPECT_TIP_ON = "Inspect mode ON — click an element on the page to attach it (Esc, or click here, to cancel).";

  var LS_AGENT = "agentbridge:agent";
  var LS_CHAT = "agentbridge:activeChat";
  var LS_AUTO = "agentbridge:autoApprove";

  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { window.localStorage.setItem(k, v); } catch (e) {} }

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
    this.scriptSrc = opts.scriptSrc || null;
    this.ws = null;
    this.connected = false;
    this.agents = [];
    this.chats = [];
    this.activeChatId = null;
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
    this._inspecting = false;
    this.autoApprove = lsGet(LS_AUTO) !== "0";  // default ON; "0" = user turned it off
    this._autoOpened = false;
    this._init();
  }

  AgentBridgeWidget.prototype._init = function () {
    var host = h("div");
    host.style.all = "initial";
    document.body.appendChild(host);
    this.hostEl = host;          // used to exclude our own UI while inspecting
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
    this.bubble.addEventListener("click", function () { self._toggle(true); });

    // Header: menu (chat list) · title · status · new chat · close
    this.statusDot = h("span", { class: "ab-status-dot" });
    this.menuBtn = h("button", { class: "ab-iconbtn", title: "Chats", text: "☰" });
    this.menuBtn.addEventListener("click", function () { self._toggleDrawer(); });
    this.newBtn = h("button", { class: "ab-iconbtn", title: "New chat", text: "+" });
    this.newBtn.addEventListener("click", function () { self._newChat(); });
    var closeBtn = h("button", { class: "ab-iconbtn", title: "Close", text: "✕" });
    closeBtn.addEventListener("click", function () { self._toggle(false); });
    var header = h("div", { class: "ab-header" }, [
      this.menuBtn,
      this.statusDot,
      h("span", { class: "ab-title", text: "Coding Agent" }),
      this.newBtn,
      closeBtn,
    ]);

    // Controls: agent picker + PR + inspect
    this.agentSelect = h("select", { class: "ab-select", title: "Default agent for new chats" });
    this.agentSelect.addEventListener("change", function () { self._selectAgent(); });
    this.prBtn = h("button", { class: "ab-btn", text: "Create PR", title: "Commit the agent's edits to a branch, open a pull request, and reset your workspace" });
    this.prBtn.addEventListener("click", function () { self._createPR(); });
    this.inspectBtn = h("button", { class: "ab-iconbtn ab-inspect ab-tip" });
    this.inspectBtn.innerHTML = INSPECT_ICON;
    this.inspectBtn.setAttribute("data-tip", INSPECT_TIP_OFF);
    this.inspectBtn.setAttribute("aria-label", INSPECT_TIP_OFF);
    this.inspectBtn.addEventListener("click", function () { self._toggleInspect(); });
    this.autoBtn = h("button", { class: "ab-iconbtn ab-autoapprove ab-tip" });
    this.autoBtn.innerHTML = SHIELD_ICON;
    this.autoBtn.addEventListener("click", function () { self._toggleAutoApprove(); });
    this.branchLabel = h("span", { class: "ab-branch-label" });
    var controls = h("div", { class: "ab-controls" }, [
      this.agentSelect, this.prBtn, this.inspectBtn, this.autoBtn, this.branchLabel,
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

    // Changed files
    this.filesHead = h("div", { class: "ab-files-head" });
    this.filesList = h("div");
    this.files = h("div", { class: "ab-files" }, [this.filesHead, this.filesList]);

    // Interactive cards (agent approval prompts, branch suggestions, PR links) dock here,
    // just above the composer — the message list itself is now React/assistant-ui.
    this.cardLayer = h("div", { class: "ab-cards" });

    // Pending attached-element chip
    this.contextBar = h("div", { class: "ab-context-bar" });

    // Composer
    this.input = h("textarea", { class: "ab-input", rows: "1", placeholder: "Describe a change…" });
    this.input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); self._sendMessage(); }
    });
    this.sendBtn = h("button", { class: "ab-send", text: "➤", title: "Send" });
    this.sendBtn.addEventListener("click", function () { self._sendMessage(); });
    var composer = h("div", { class: "ab-composer" }, [this.input, this.sendBtn]);

    // Connection banner (shown when not connected to the server)
    this.bannerText = h("span", { class: "ab-banner-text" });
    this.bannerRetry = h("button", { class: "ab-banner-retry", text: "Retry now" });
    this.bannerRetry.addEventListener("click", function () { self._retryNow(); });
    this.banner = h("div", { class: "ab-banner" }, [
      h("span", { class: "ab-banner-dot" }), this.bannerText, this.bannerRetry,
    ]);

    var body = h("div", { class: "ab-body" }, [this.messages, this.drawer]);
    var panel = h("div", { class: "ab-panel" }, [header, this.banner, controls, body, this.files, this.cardLayer, this.contextBar, composer]);
    this.root.appendChild(this.bubble);
    this.root.appendChild(panel);
    this.shadow.appendChild(this.root);

    this._setConnState("connecting");
    this._setChatActive(false);
  };

  AgentBridgeWidget.prototype._toggle = function (open) {
    this.root.classList.toggle("open", open);
    if (open) this.input.focus();
    else if (this._inspecting) this._stopInspect();
  };

  AgentBridgeWidget.prototype._toggleDrawer = function (force) {
    var open = force === undefined ? !this.drawer.classList.contains("open") : force;
    this.drawer.classList.toggle("open", open);
  };
  AgentBridgeWidget.prototype._closeDrawer = function () { this._toggleDrawer(false); };

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
      case "pr_created": return this._isActive(msg) && this._prLink(msg.url, msg.number);
      case "status": return this._isActive(msg) && this._onStatus(msg.state);
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
      var item = h("div", { class: "ab-chat-item" + (c.id === self.activeChatId ? " active" : "") });
      var main = h("div", { class: "ab-chat-main" }, [
        h("div", { class: "ab-chat-title", text: c.title || "New chat" }),
        h("div", { class: "ab-chat-meta", text: c.agent + " · " + c.message_count + " msg" }),
      ]);
      main.addEventListener("click", function () { self._openChat(c.id); });
      var del = h("button", { class: "ab-chat-del", title: "Delete chat", text: "🗑" });
      del.addEventListener("click", function (e) { e.stopPropagation(); self._deleteChat(c.id); });
      item.appendChild(main);
      item.appendChild(del);
      self.drawerList.appendChild(item);
    });
  };

  // ---- Chat lifecycle --------------------------------------------------- //

  AgentBridgeWidget.prototype._selectAgent = function () {
    this.selectedAgent = this.agentSelect.value || null;
    if (this.selectedAgent) lsSet(LS_AGENT, this.selectedAgent);
    this._applyTheme();
    // First-run convenience: if nothing is open yet, start a chat right away.
    if (this.selectedAgent && !this.activeChatId) this._newChat();
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
    this.bridge.addUser(text);
    this.input.value = "";
    var context = { page: this._collectPageContext() };
    if (this.pendingElement) context.element = this.pendingElement;
    this._send({
      type: "user_message", chat_id: this.activeChatId, text: text,
      context: context, auto_approve: this.autoApprove,
    });
    this._clearPendingElement();
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
    var title = (this.input.value.trim()) || ("AgentBridge: " + (this.sessionAgent || "session") + " changes");
    this.input.value = "";
    this._send({ type: "create_pr", chat_id: this.activeChatId, title: title });
    this._system("Creating pull request…");
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
      source: this._sourceHint(el),
      attributes: attrs
    };
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
    try {
      var key = Object.keys(el).find(function (k) { return k.indexOf("__reactFiber$") === 0; });
      if (!key) return null;
      var fiber = el[key];
      for (var d = 0; fiber && d < 40; d++, fiber = fiber.return) {
        var src = fiber._debugSource;
        if (src && src.fileName) return { file: src.fileName, line: src.lineNumber || null };
      }
    } catch (e) {}
    return null;
  };

  // ---- Pending-element chip --------------------------------------------- //

  AgentBridgeWidget.prototype._setPendingElement = function (desc) {
    this.pendingElement = desc;
    this.contextBar.innerHTML = "";
    var self = this;
    var label = desc.component ? "‹" + desc.component + "› " + desc.label : desc.label;
    var chip = h("span", { class: "ab-chip" }, [
      h("span", { class: "ab-chip-icon", text: "🎯" }),
      h("span", { class: "ab-chip-text", title: desc.selector, text: label }),
    ]);
    var x = h("button", { class: "ab-chip-x", title: "Remove", text: "✕" });
    x.addEventListener("click", function () { self._clearPendingElement(); });
    chip.appendChild(x);
    this.contextBar.appendChild(chip);
    this.contextBar.classList.add("show");
  };

  AgentBridgeWidget.prototype._clearPendingElement = function () {
    this.pendingElement = null;
    this.contextBar.innerHTML = "";
    this.contextBar.classList.remove("show");
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

    if (msg.options && msg.options.length) {
      var buttons = msg.options.map(function (opt, i) {
        var b = h("button", { class: "ab-btn" + (i === 0 ? " primary" : ""), text: opt });
        b.addEventListener("click", function () { reply(opt); });
        return b;
      });
      var card = this._card("Agent needs your approval", msg.prompt, buttons);
      return;
    }

    var input = h("input", { class: "ab-prompt-input ab-select", type: "text", placeholder: "Your answer…" });
    var send = h("button", { class: "ab-btn primary", text: "Reply" });
    send.addEventListener("click", function () { reply(input.value); });
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") reply(input.value); });
    var card = this._card("Agent needs input", msg.prompt, [send]);
    card.insertBefore(input, card.querySelector(".ab-card-actions"));
    input.focus();
  };

  AgentBridgeWidget.prototype._onFileChanges = function (files) {
    this.filesList.innerHTML = "";
    if (!files || !files.length) { this.files.classList.remove("show"); return; }
    this.filesHead.textContent = files.length + " changed file" + (files.length > 1 ? "s" : "");
    var self = this;
    files.forEach(function (f) {
      self.filesList.appendChild(h("div", { class: "ab-file" }, [
        h("span", { class: "code", text: f.status }),
        h("span", { text: f.path }),
      ]));
    });
    this.files.classList.add("show");
  };

  AgentBridgeWidget.prototype._onStatus = function (state) {
    var working = state === "working";
    this.statusDot.classList.toggle("working", working);
    this.sendBtn.disabled = working;
    this.bridge.setRunning(working, this._agentLabel(this._activeAgentName()));
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
    this.files.classList.remove("show");
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
    if (!this.root) return;
    var theme = this._agentTheme(this._activeAgentName());
    var vars = { accent: "--ab-accent", accentFg: "--ab-accent-fg" };
    for (var key in vars) {
      if (theme && theme[key]) this.root.style.setProperty(vars[key], theme[key]);
      else this.root.style.removeProperty(vars[key]);
    }
  };

  AgentBridgeWidget.prototype._updateBranchLabel = function () {
    var b = this.targetBranch || this.branch;
    this.branchLabel.textContent = b ? "⛓ " + b : "";
  };

  AgentBridgeWidget.prototype._setConnected = function (on) {
    this.menuBtn.disabled = !on;
    this.newBtn.disabled = !on;
    this.agentSelect.disabled = !on;
    if (!on) this._setChatActive(false);
  };

  AgentBridgeWidget.prototype._setChatActive = function (on) {
    this.prBtn.disabled = !on;
    this.inspectBtn.disabled = !on;
    this.sendBtn.disabled = !on;
    this.input.disabled = !on;
  };

  // Scrolling is handled by assistant-ui's <ThreadViewport> auto-scroll; kept as a no-op
  // so any stray caller is harmless.
  AgentBridgeWidget.prototype._scroll = function () {};

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // ---- Public API + auto-init ------------------------------------------ //

  var AgentBridge = {
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
      scriptSrc: current.src,
    });
  }

  window.AgentBridge = AgentBridge;
})();
