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
 */
(function () {
  "use strict";

  // The build step replaces this placeholder with the contents of agentbridge-widget.css.
  // When running un-built from /widget-src, we fall back to fetching the sibling .css.
  var STYLES = "/*__INJECT_CSS__*/";

  var ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';

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
    this.sessionAgent = null;
    this.branch = null;
    this.currentAgentMsg = null; // accumulating agent bubble for the active turn
    this.reconnectDelay = 1000;
    this._init();
  }

  AgentBridgeWidget.prototype._init = function () {
    var host = h("div");
    host.style.all = "initial";
    document.body.appendChild(host);
    this.shadow = host.attachShadow({ mode: "open" });

    var style = document.createElement("style");
    this.shadow.appendChild(style);
    if (STYLES.indexOf("__INJECT_CSS__") !== -1 && this.scriptSrc) {
      // Un-built: pull the sibling stylesheet.
      var cssUrl = this.scriptSrc.replace(/[^/]*$/, "agentbridge-widget.css");
      fetch(cssUrl).then(function (r) { return r.text(); }).then(function (t) { style.textContent = t; });
    } else {
      style.textContent = STYLES;
    }

    this._buildUI();
    this._connect();
  };

  AgentBridgeWidget.prototype._buildUI = function () {
    var self = this;

    this.root = h("div", { class: "ab-root " + this.position });

    // Launcher bubble
    this.bubble = h("button", { class: "ab-bubble", title: "Ask a coding agent", "aria-label": "Open agent chat" });
    this.bubble.innerHTML = ICON;
    this.bubble.addEventListener("click", function () { self._toggle(true); });

    // Panel
    this.statusDot = h("span", { class: "ab-status-dot" });
    var closeBtn = h("button", { class: "ab-iconbtn", title: "Close", text: "✕" });
    closeBtn.addEventListener("click", function () { self._toggle(false); });
    var header = h("div", { class: "ab-header" }, [
      this.statusDot,
      h("span", { class: "ab-title", text: "Coding Agent" }),
      closeBtn,
    ]);

    // Controls: agent picker + branch button
    this.agentSelect = h("select", { class: "ab-select", title: "Choose an agent" });
    this.agentSelect.addEventListener("change", function () { self._startSession(); });
    this.branchBtn = h("button", { class: "ab-btn", text: "Branch" , title: "Create a git branch for this session"});
    this.branchBtn.addEventListener("click", function () { self._requestBranch(); });
    this.prBtn = h("button", { class: "ab-btn", text: "Create PR", title: "Commit, push and open a pull request" });
    this.prBtn.addEventListener("click", function () { self._createPR(); });
    this.branchLabel = h("span", { class: "ab-branch-label" });
    var controls = h("div", { class: "ab-controls" }, [
      this.agentSelect, this.branchBtn, this.prBtn, this.branchLabel,
    ]);

    // Messages
    this.messages = h("div", { class: "ab-messages" });

    // Changed files
    this.filesHead = h("div", { class: "ab-files-head" });
    this.filesList = h("div");
    this.files = h("div", { class: "ab-files" }, [this.filesHead, this.filesList]);

    // Composer
    this.input = h("textarea", { class: "ab-input", rows: "1", placeholder: "Describe a change…" });
    this.input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); self._sendMessage(); }
    });
    this.sendBtn = h("button", { class: "ab-send", text: "➤", title: "Send" });
    this.sendBtn.addEventListener("click", function () { self._sendMessage(); });
    var composer = h("div", { class: "ab-composer" }, [this.input, this.sendBtn]);

    var panel = h("div", { class: "ab-panel" }, [header, controls, this.messages, this.files, composer]);
    this.root.appendChild(this.bubble);
    this.root.appendChild(panel);
    this.shadow.appendChild(this.root);

    this._setControlsEnabled(false);
  };

  AgentBridgeWidget.prototype._toggle = function (open) {
    this.root.classList.toggle("open", open);
    if (open) this.input.focus();
  };

  // ---- WebSocket -------------------------------------------------------- //

  AgentBridgeWidget.prototype._connect = function () {
    var self = this;
    try {
      this.ws = new WebSocket(this.server);
    } catch (e) {
      this._system("Could not connect to " + this.server);
      return;
    }
    this.ws.addEventListener("open", function () {
      self.connected = true;
      self.reconnectDelay = 1000;
      self.statusDot.classList.add("connected");
      self._send({ type: "list_agents" });
    });
    this.ws.addEventListener("message", function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      self._onMessage(msg);
    });
    this.ws.addEventListener("close", function () {
      self.connected = false;
      self.statusDot.classList.remove("connected", "working");
      self._setControlsEnabled(false);
      // Auto-reconnect with backoff.
      setTimeout(function () { self._connect(); }, self.reconnectDelay);
      self.reconnectDelay = Math.min(self.reconnectDelay * 2, 15000);
    });
    this.ws.addEventListener("error", function () { try { self.ws.close(); } catch (e) {} });
  };

  AgentBridgeWidget.prototype._send = function (obj) {
    if (this.ws && this.connected) this.ws.send(JSON.stringify(obj));
  };

  AgentBridgeWidget.prototype._onMessage = function (msg) {
    switch (msg.type) {
      case "agents": return this._onAgents(msg.agents);
      case "session_started":
        this.sessionAgent = msg.agent;
        this.branch = msg.branch;
        this._updateBranchLabel();
        this._setControlsEnabled(true);
        this._system("Session started with " + msg.agent + " on " + msg.branch);
        return;
      case "agent_chunk": return this._onChunk(msg);
      case "agent_prompt": return this._onPrompt(msg);
      case "branch_suggested": return this._onBranchSuggested(msg);
      case "branch_created":
        this.branch = msg.branch; this._updateBranchLabel();
        this._system("Branch created: " + msg.branch);
        return;
      case "file_changes": return this._onFileChanges(msg.files);
      case "pr_created":
        this._prLink(msg.url, msg.number);
        return;
      case "status": return this._onStatus(msg.state);
      case "error": return this._addMsg("error", msg.message);
    }
  };

  AgentBridgeWidget.prototype._onAgents = function (agents) {
    var self = this;
    this.agentSelect.innerHTML = "";
    var placeholder = h("option", { value: "", text: "Choose agent…" });
    placeholder.disabled = true; placeholder.selected = true;
    this.agentSelect.appendChild(placeholder);
    agents.forEach(function (a) {
      var opt = h("option", { value: a.name, text: a.label + (a.available ? "" : " (unavailable)") });
      if (!a.available) opt.disabled = true;
      self.agentSelect.appendChild(opt);
    });
  };

  // ---- Actions ---------------------------------------------------------- //

  AgentBridgeWidget.prototype._startSession = function () {
    var agent = this.agentSelect.value;
    if (!agent) return;
    this.messages.innerHTML = "";
    this._send({ type: "start_session", agent: agent });
  };

  AgentBridgeWidget.prototype._sendMessage = function () {
    var text = this.input.value.trim();
    if (!text) return;
    if (!this.sessionAgent) { this._system("Pick an agent to start a session first."); return; }
    this._addMsg("user", text);
    this.input.value = "";
    this.currentAgentMsg = null;
    this._send({ type: "user_message", text: text });
  };

  AgentBridgeWidget.prototype._requestBranch = function (suggestedName) {
    this._send({ type: "create_branch", name: suggestedName || null });
  };

  AgentBridgeWidget.prototype._createPR = function () {
    var title = (this.input.value.trim()) || ("AgentBridge: " + (this.sessionAgent || "session") + " changes");
    this.input.value = "";
    this._send({ type: "create_pr", title: title });
    this._system("Creating pull request…");
  };

  // ---- Rendering -------------------------------------------------------- //

  AgentBridgeWidget.prototype._onChunk = function (msg) {
    var cls = msg.stream === "thinking" ? "agent thinking" : "agent";
    if (!this.currentAgentMsg || this.currentAgentMsg._stream !== msg.stream) {
      this.currentAgentMsg = this._addMsg(cls, "");
      this.currentAgentMsg._stream = msg.stream;
    }
    this.currentAgentMsg.textContent += msg.text;
    this._scroll();
  };

  AgentBridgeWidget.prototype._onPrompt = function (msg) {
    var self = this;
    function reply(answer) {
      self._send({ type: "agent_response", request_id: msg.request_id, answer: answer });
      card.remove();
    }

    if (msg.options && msg.options.length) {
      // Button-per-option (e.g. Allow / Deny).
      var buttons = msg.options.map(function (opt, i) {
        var b = h("button", { class: "ab-btn" + (i === 0 ? " primary" : ""), text: opt });
        b.addEventListener("click", function () { reply(opt); });
        return b;
      });
      var card = this._card("Agent needs your approval", msg.prompt, buttons);
      return;
    }

    // Free-text reply.
    var input = h("input", { class: "ab-prompt-input ab-select", type: "text", placeholder: "Your answer…" });
    var send = h("button", { class: "ab-btn primary", text: "Reply" });
    send.addEventListener("click", function () { reply(input.value); });
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") reply(input.value); });
    var card = this._card("Agent needs input", msg.prompt, [send]);
    card.insertBefore(input, card.querySelector(".ab-card-actions"));
    input.focus();
  };

  AgentBridgeWidget.prototype._onBranchSuggested = function (msg) {
    var self = this;
    var create = h("button", { class: "ab-btn primary", text: "Create branch" });
    create.addEventListener("click", function () {
      self._requestBranch(msg.suggested_name);
      card.remove();
    });
    var ignore = h("button", { class: "ab-btn", text: "Not now" });
    ignore.addEventListener("click", function () { card.remove(); });
    var body = h("span");
    body.innerHTML = msg.reason + " &nbsp;<code>" + escapeHtml(msg.suggested_name) + "</code>";
    var card = this._card("Branch out?", null, [create, ignore]);
    card.querySelector(".ab-card-body").appendChild(body);
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
    this.statusDot.classList.toggle("working", state === "working");
    this.sendBtn.disabled = state === "working";
  };

  AgentBridgeWidget.prototype._prLink = function (url, number) {
    var link = h("a", { href: url, target: "_blank", text: "View PR" + (number ? " #" + number : "") });
    link.style.color = "var(--ab-accent)";
    var card = this._card("Pull request created", null, []);
    card.querySelector(".ab-card-body").appendChild(link);
  };

  // ---- DOM helpers ------------------------------------------------------ //

  AgentBridgeWidget.prototype._addMsg = function (cls, text) {
    var el = h("div", { class: "ab-msg " + cls, text: text });
    this.messages.appendChild(el);
    this._scroll();
    return el;
  };

  AgentBridgeWidget.prototype._system = function (text) { this._addMsg("system", text); };

  AgentBridgeWidget.prototype._card = function (title, bodyText, actions) {
    var card = h("div", { class: "ab-card" }, [
      h("div", { class: "ab-card-title", text: title }),
      h("div", { class: "ab-card-body", text: bodyText || "" }),
    ]);
    var actionsRow = h("div", { class: "ab-card-actions" }, actions || []);
    card.appendChild(actionsRow);
    this.messages.appendChild(card);
    this._scroll();
    return card;
  };

  AgentBridgeWidget.prototype._updateBranchLabel = function () {
    this.branchLabel.textContent = this.branch ? "⛓ " + this.branch : "";
  };

  AgentBridgeWidget.prototype._setControlsEnabled = function (on) {
    this.branchBtn.disabled = !on;
    this.prBtn.disabled = !on;
    this.sendBtn.disabled = !on;
  };

  AgentBridgeWidget.prototype._scroll = function () {
    this.messages.scrollTop = this.messages.scrollHeight;
  };

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

  // Auto-init from the script tag's data-* attributes.
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
