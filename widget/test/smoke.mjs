/*
 * Smoke test: load the built bundle in jsdom, mount the widget, drive the bridge as the
 * WebSocket layer would, and assert assistant-ui renders the messages (incl. markdown).
 *
 *   node test/smoke.mjs
 */

import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";
import assert from "node:assert";
import { TransformStream, ReadableStream, WritableStream } from "node:stream/web";

const bundle = readFileSync(new URL("../dist/agentbridge-widget.js", import.meta.url), "utf8");

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  runScripts: "outside-only",
  pretendToBeVisual: true,
});
const { window } = dom;

// Polyfills assistant-ui / React expect in a browser but jsdom lacks.
class RO { observe() {} unobserve() {} disconnect() {} }
window.ResizeObserver = RO;
window.Element.prototype.scrollTo = function () {}; // jsdom lacks it; assistant-ui's viewport calls it
window.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } };
window.matchMedia = window.matchMedia || function () {
  return { matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} };
};
// A no-op WebSocket so _connect() doesn't throw; we drive the bridge directly instead.
window.WebSocket = class { constructor() {} addEventListener() {} send() {} close() {} };
// Web Streams — native in browsers, absent in jsdom; assistant-ui's stream core needs them.
window.TransformStream = TransformStream;
window.ReadableStream = ReadableStream;
window.WritableStream = WritableStream;

// Execute the IIFE bundle in the jsdom window context (globals resolve to `window`).
try {
  window.eval(bundle);
} catch (e) {
  console.error("EVAL FAIL:", e && e.message);
  console.error((e && e.stack ? String(e.stack).split("\n").slice(0, 4).join("\n") : "").slice(0, 600));
  process.exit(1);
}

const AgentBridge = window.AgentBridge;
assert.ok(AgentBridge && typeof AgentBridge.init === "function", "AgentBridge.init missing");
// A real build stamp (not the unbundled "dev" fallback) so we can verify deployed builds.
assert.ok(AgentBridge.version && AgentBridge.version !== "dev", "widget version not stamped: " + AgentBridge.version);

const widget = AgentBridge.init({ server: "ws://localhost:9/ws" });
assert.ok(widget && widget.shadow, "widget did not mount a shadow root");
// On init the widget has a real corner anchor applied (not just the CSS default), so the first
// open positions identically to every later one — no off-screen flash / auto-correct.
assert.ok(widget._anchor && widget._anchor.h && widget._anchor.v, "no default anchor on init");
assert.ok(widget.root.style.bottom !== "auto" && widget.root.style.bottom !== "", "default anchor not applied to root");
assert.equal(widget.hostEl.getAttribute("data-ab-version"), AgentBridge.version, "host data-ab-version mismatch");
const verEl = widget.shadow.querySelector(".ab-ver");
assert.ok(verEl && verEl.textContent === AgentBridge.version, "version not shown in header: " + (verEl && verEl.textContent));

const tick = () => new Promise((r) => setTimeout(r, 60));

function text() { return widget.shadow.textContent || ""; }
function html() { return widget.shadow.querySelector(".ab-messages").innerHTML; }

async function main() {
  await tick();
  // Empty state visible before any messages.
  widget.bridge.setEmptyHint("Pick an agent above and press + to start a chat.");
  await tick();
  assert.ok(widget.shadow.querySelector(".ab-empty"), "empty state not rendered");

  // User message + streamed agent markdown, as the WS handlers would push.
  widget.bridge.addUser("make the button **blue**");
  widget.bridge.chunk("## Plan\n\nI'll edit `Button.tsx`:\n\n- step one\n- step two\n", "stdout");
  widget.bridge.chunk("\nDone.", "stdout");
  widget.bridge.setRunning(false);
  await tick();
  await tick();

  const t = text();
  assert.ok(widget.shadow.querySelector(".ab-msg.user"), "user message not rendered");
  assert.ok(widget.shadow.querySelector(".ab-msg.agent"), "agent message not rendered");
  assert.ok(/make the button/.test(t), "user text missing");

  const agentHtml = widget.shadow.querySelector(".ab-msg.agent").innerHTML;
  assert.ok(/<strong>blue<\/strong>/.test(widget.shadow.querySelector(".ab-msg.user").innerHTML),
    "user markdown bold not rendered");
  assert.ok(/<h2[ >]/.test(agentHtml), "agent markdown heading not rendered: " + agentHtml.slice(0, 200));
  assert.ok(/<code[ >]/.test(agentHtml), "agent inline code not rendered");
  assert.ok(/<li[ >]/.test(agentHtml), "agent list not rendered");
  assert.ok(/Done\./.test(agentHtml), "streamed continuation missing");

  // A "thinking" stream renders as a reasoning part.
  widget.bridge.chunk("considering the layout", "thinking");
  await tick();
  const reasoning = widget.shadow.querySelector(".ab-reasoning");
  assert.ok(reasoning && /considering the layout/.test(reasoning.textContent),
    "thinking/reasoning not rendered");

  // Internal "thinking" activity overwrites in place — only the latest step shows, and there
  // is just one reasoning bubble (successive tool calls / thoughts don't pile up).
  widget.bridge.reset();
  widget.bridge.chunk("Reading a.ts", "thinking");
  widget.bridge.chunk("Editing a.ts", "thinking");
  await tick();
  const reasons = widget.shadow.querySelectorAll(".ab-reasoning");
  assert.equal(reasons.length, 1, "thinking should stay in a single overwriting bubble");
  assert.ok(/Editing a\.ts/.test(reasons[0].textContent) && !/Reading a\.ts/.test(reasons[0].textContent),
    "thinking should overwrite, showing only the latest step");
  widget.bridge.reset();

  // clearThinking drops the transient progress but keeps the real answer.
  widget.bridge.addUser("rename it");
  widget.bridge.chunk("Editing useOpportunityStats.ts", "thinking");
  widget.bridge.addAgent("Done — renamed to Admin.");
  await tick();
  assert.ok(widget.shadow.querySelector(".ab-reasoning"), "thinking should be present mid-turn");
  widget.bridge.clearThinking();
  await tick();
  assert.ok(!widget.shadow.querySelector(".ab-reasoning"), "thinking should be cleared");
  assert.ok(/Done — renamed to Admin\./.test(widget.shadow.textContent), "answer should remain after clearThinking");
  widget.bridge.reset();

  // While working and awaiting the reply, an animated "<Agent> is thinking" indicator shows;
  // it disappears once the assistant starts streaming, and when work stops.
  widget.bridge.reset();
  widget.bridge.addUser("do a thing");
  widget.bridge.setRunning(true, "Claude Code");
  await tick();
  const typing = widget.shadow.querySelector(".ab-typing");
  assert.ok(typing && /Claude Code is thinking/.test(typing.textContent), "thinking indicator missing");
  assert.ok(widget.shadow.querySelector(".ab-typing-dots span"), "thinking indicator dots missing");
  widget.bridge.chunk("on it", "stdout");   // reply starts → indicator goes away
  await tick();
  assert.ok(!widget.shadow.querySelector(".ab-typing"), "thinking indicator should hide once streaming");
  widget.bridge.setRunning(false);
  widget.bridge.reset();

  // A system note routes to a system message.
  widget.bridge.addSystem("Target branch: feature/x");
  await tick();
  assert.ok(/Target branch: feature\/x/.test(text()), "system note missing");

  // Reset clears the thread back to empty.
  widget.bridge.reset();
  widget.bridge.setEmptyHint("empty again");
  await tick();
  assert.ok(!widget.shadow.querySelector(".ab-msg"), "messages not cleared on reset");

  // Both icon buttons carry a hover tooltip (data-tip) describing their state.
  var auto = widget.shadow.querySelector(".ab-autoapprove.ab-tip");
  assert.ok(auto, "auto-approve button missing the ab-tip class");
  assert.ok(/Auto-approve is (ON|OFF)/.test(auto.getAttribute("data-tip") || ""),
    "auto-approve tooltip (data-tip) not set");
  var inspect = widget.shadow.querySelector(".ab-inspect.ab-tip");
  assert.ok(inspect, "inspect button missing the ab-tip class");
  assert.ok(/Inspect mode/.test(inspect.getAttribute("data-tip") || ""),
    "inspect tooltip (data-tip) not set");

  // The new-chat button is a labeled pill (clearer than a bare "+").
  var newBtn = widget.shadow.querySelector(".ab-newbtn");
  assert.ok(newBtn && /New chat/.test(newBtn.textContent), "new-chat button not labeled");

  // Changed-files area: hidden until there are changes, then a collapsible header whose list
  // is hidden by default and toggles open.
  var filesEl = widget.shadow.querySelector(".ab-files");
  assert.ok(!filesEl.classList.contains("show"), "files area should start hidden");
  widget._onFileChanges([{ path: "apps/dashboards/app/recommendations/useOpportunityStats.ts", status: "M" }]);
  assert.ok(filesEl.classList.contains("show"), "files area should show when there are changes");
  assert.ok(!filesEl.classList.contains("open"), "files list should be collapsed by default");
  assert.ok(/changed file/.test(widget.shadow.querySelector(".ab-files-head").textContent), "files header missing count");
  widget._toggleFiles();
  assert.ok(filesEl.classList.contains("open"), "files list should expand on toggle");
  widget._onFileChanges([]);
  assert.ok(!filesEl.classList.contains("show"), "files area should hide with no changes");

  // --- Stop + queue: while the agent is busy, the Stop button shows and follow-ups queue ---
  const sent = [];
  widget._send = (m) => sent.push(m);
  widget.activeChatId = "c1";
  const stopBtn = widget.shadow.querySelector(".ab-stop");
  const queueEl = widget.shadow.querySelector(".ab-queue");
  assert.ok(stopBtn && stopBtn.hidden, "stop button should be hidden when idle");

  widget._onStatus("working");
  assert.ok(!stopBtn.hidden, "stop button should show while working");

  // Typing + send while working queues instead of dispatching.
  widget.input.value = "also rename the header";
  widget._sendMessage();
  assert.equal(widget.queue.length, 1, "follow-up should be queued while working");
  assert.ok(queueEl.classList.contains("show") && /1 queued/.test(queueEl.textContent), "queue strip not shown");
  assert.ok(!sent.some((m) => m.type === "user_message"), "queued message must not be sent yet");

  // Stop interrupts and clears the queue.
  widget._stopAgent();
  assert.ok(sent.some((m) => m.type === "stop" && m.chat_id === "c1"), "stop message not sent");
  assert.equal(widget.queue.length, 0, "stop should clear the queue");

  // Queue again, then going idle drains one queued follow-up as a real user_message.
  widget._onStatus("working");
  widget.input.value = "and fix the spacing";
  widget._sendMessage();
  assert.equal(widget.queue.length, 1, "second follow-up should queue");
  widget._onStatus("idle");
  assert.equal(widget.queue.length, 0, "idle should drain the queue");
  const um = sent.filter((m) => m.type === "user_message");
  assert.ok(um.length === 1 && /fix the spacing/.test(um[0].text), "dequeued message not dispatched");
  assert.ok(stopBtn.hidden, "stop button should hide when idle");

  // --- Attachments: a stored upload becomes a ready chip and rides along on the next send ---
  widget.attachments = []; widget._renderContextBar();
  widget.attachments.push({ id: "u1", name: "spec.md", size: 6, status: "uploading", path: null });
  widget._renderContextBar();
  const ctxBar = widget.shadow.querySelector(".ab-context-bar");
  assert.ok(ctxBar.classList.contains("show") && widget.shadow.querySelector(".ab-chip-file"),
    "attachment chip not shown while uploading");
  // While an upload is in flight, sending is blocked.
  sent.length = 0;
  widget.input.value = "use the spec";
  widget._sendMessage();
  assert.ok(!sent.some((m) => m.type === "user_message"), "must not send while an upload is in flight");
  // Upload result marks it ready; the path then rides along on the next message and clears.
  widget._onFileUploaded({ upload_id: "u1", ok: true, name: "spec.md", path: ".agentbridge/uploads/c1/spec.md", size: 6 });
  assert.equal(widget.attachments[0].status, "ready", "upload result should mark the chip ready");
  widget.input.value = "use the spec";
  widget._sendMessage();
  const umsg = sent.find((m) => m.type === "user_message");
  assert.ok(umsg && umsg.attachments && umsg.attachments[0] === ".agentbridge/uploads/c1/spec.md",
    "attachment path not sent with the message");
  assert.equal(widget.attachments.length, 0, "attachments should clear after send");
  assert.ok(!ctxBar.classList.contains("show"), "context bar should hide after send");

  widget.queue = []; widget._renderQueue(); widget.activeChatId = null;

  // --- Modes: the picker shows only for agents that support it, and rides on the message ---
  widget._onAgents([
    { name: "claude-code", label: "Claude Code", available: true, capabilities: { plan_mode: true } },
    { name: "cursor", label: "Cursor", available: true, capabilities: {} },
  ]);
  const modeSel = widget.shadow.querySelector(".ab-mode");
  assert.ok(modeSel, "mode select missing");
  widget.sessionAgent = "claude-code"; widget.selectedAgent = "claude-code"; widget._applyTheme();
  assert.ok(!modeSel.hidden, "mode picker should show for a plan-capable agent");
  widget.sessionAgent = "cursor"; widget.selectedAgent = "cursor"; widget._applyTheme();
  assert.ok(modeSel.hidden, "mode picker should hide for an agent without plan mode");
  // Plan mode rides along on the message for a capable agent; default mode is omitted.
  widget.sessionAgent = "claude-code"; widget.selectedAgent = "claude-code"; widget._applyTheme();
  sent.length = 0; widget.activeChatId = "c1"; widget.running = false;
  widget.mode = "plan"; widget.modeSelect.value = "plan";
  widget.input.value = "plan the thing"; widget._sendMessage();
  let pmsg = sent.find((m) => m.type === "user_message");
  assert.equal(pmsg && pmsg.mode, "plan", "plan mode should be sent with the message");
  sent.length = 0; widget.mode = "default"; widget.modeSelect.value = "default";
  widget.input.value = "code the thing"; widget._sendMessage();
  pmsg = sent.find((m) => m.type === "user_message");
  assert.ok(pmsg && pmsg.mode === undefined, "default mode should not be sent");
  widget.activeChatId = null; widget.sessionAgent = null; widget.selectedAgent = null;

  // --- Plugins (MCP): open the panel, list servers, add via the form, toggle, delete ---
  sent.length = 0;
  widget._togglePlugins(true);
  const pluginsPanel = widget.shadow.querySelector(".ab-plugins");
  assert.ok(pluginsPanel.classList.contains("open"), "plugins panel should open");
  widget._onMcpServers([{ name: "figma", transport: "sse", url: "http://127.0.0.1:3845/sse", enabled: true }]);
  assert.ok(/figma/.test(widget.shadow.querySelector(".ab-plugins-list").textContent), "registered plugin not listed");
  const pToggle = widget.shadow.querySelector(".ab-plugin-toggle");
  assert.ok(pToggle && pToggle.checked, "enabled plugin should show a checked toggle");
  pToggle.checked = false; pToggle.dispatchEvent(new window.Event("change"));
  assert.ok(sent.some((m) => m.type === "toggle_mcp" && m.name === "figma" && m.enabled === false),
    "toggling a plugin should send toggle_mcp");
  // The form: open it, fill a stdio server, save -> save_mcp with parsed args.
  widget._openPluginForm(null);
  const inputs = widget.shadow.querySelectorAll(".ab-plugin-form .ab-plugin-input");
  inputs[0].value = "db";                       // name
  widget.shadow.querySelectorAll(".ab-plugin-form .ab-plugin-input")[2].value = "db-mcp";  // command
  widget.shadow.querySelectorAll(".ab-plugin-form .ab-plugin-input")[3].value = "--port 5432"; // args
  widget.shadow.querySelector(".ab-plugin-form-actions .ab-btn").click();   // Save
  const saveMsg = sent.find((m) => m.type === "save_mcp");
  assert.ok(saveMsg && saveMsg.server.name === "db" && saveMsg.server.command === "db-mcp", "save_mcp not sent");
  assert.deepEqual(saveMsg.server.args, ["--port", "5432"], "args not parsed into a list");
  // Delete sends delete_mcp.
  widget.shadow.querySelector(".ab-plugin-row .ab-chip-x").click();
  assert.ok(sent.some((m) => m.type === "delete_mcp" && m.name === "figma"), "delete_mcp not sent");
  widget._togglePlugins(false);
  assert.ok(!pluginsPanel.classList.contains("open"), "plugins panel should close");

  // Toggling auto-approve drops a system note into the chat (on, then off).
  widget.bridge.reset();
  widget._toggleAutoApprove();
  widget._toggleAutoApprove();
  await tick();
  var sysText = widget.shadow.querySelector(".ab-thread").textContent || "";
  assert.ok(/Auto-approve on/.test(sysText), "auto-approve ON note missing");
  assert.ok(/Auto-approve off/.test(sysText), "auto-approve OFF note missing");

  // Turning inspect mode on and off posts matching notes (no element attached → "off").
  widget.bridge.reset();
  widget._toggleInspect();   // on
  widget._stopInspect();     // off
  await tick();
  var insText = widget.shadow.querySelector(".ab-thread").textContent || "";
  assert.ok(/Inspect mode on/i.test(insText), "inspect ON note missing");
  assert.ok(/Inspect mode off/i.test(insText), "inspect OFF note missing");
  widget.bridge.reset();

  // --- Per-agent theming: the active agent's accent flows to the CSS vars the thread uses ---
  widget._onAgents([
    { name: "claude-code", label: "Claude Code", available: true, theme: { accent: "#d97757", accentFg: "#ffffff" } },
    { name: "cursor", label: "Cursor", available: true, theme: { accent: "#111827", accentFg: "#ffffff" } },
  ]);
  widget.selectedAgent = "claude-code";
  widget._applyTheme();
  assert.equal(widget.root.style.getPropertyValue("--ab-accent"), "#d97757", "picker agent accent not applied");

  // An open chat's agent wins over the picker selection.
  widget.sessionAgent = "cursor";
  widget._applyTheme();
  assert.equal(widget.root.style.getPropertyValue("--ab-accent"), "#111827", "session agent accent not applied");

  // No active agent (or one without a theme) falls back to the default accent.
  widget.sessionAgent = null;
  widget.selectedAgent = null;
  widget._applyTheme();
  assert.equal(widget.root.style.getPropertyValue("--ab-accent"), "", "accent not reset to default");

  // --- Draggable: dragging the header pins the widget at an explicit left/top ---
  const headerEl = widget.shadow.querySelector(".ab-header.ab-drag");
  assert.ok(headerEl, "header drag handle missing");
  const md = new window.MouseEvent("mousedown", { bubbles: true, button: 0, clientX: 100, clientY: 100 });
  headerEl.dispatchEvent(md);
  window.document.dispatchEvent(new window.MouseEvent("mousemove", { bubbles: true, clientX: 160, clientY: 140 }));
  window.document.dispatchEvent(new window.MouseEvent("mouseup", { bubbles: true }));
  assert.ok(widget.root.style.left && widget.root.style.top, "drag did not pin left/top");
  assert.equal(widget.root.style.right, "auto", "drag should drop the corner anchor");

  // The bubble is draggable too, but a drag must NOT trigger its click-to-open; a plain click must.
  const mouse = (type, x, y) => new window.MouseEvent(type, { bubbles: true, button: 0, clientX: x, clientY: y });
  widget._toggle(false);                                  // collapse to the bubble
  const bubble = widget.shadow.querySelector(".ab-bubble");
  bubble.dispatchEvent(mouse("mousedown", 100, 100));
  window.document.dispatchEvent(mouse("mousemove", 200, 180));
  window.document.dispatchEvent(mouse("mouseup", 200, 180));
  assert.ok(widget._dragJustHappened, "bubble drag should flag a click to suppress");
  bubble.dispatchEvent(mouse("click", 200, 180));
  assert.ok(!widget.root.classList.contains("open"), "a drag must not open the chat");
  bubble.dispatchEvent(mouse("click", 200, 180));         // a real click (no preceding drag)
  assert.ok(widget.root.classList.contains("open"), "a plain click should open the chat");

  // Dropping near a corner must anchor by that corner (right/bottom), so the panel opens inward
  // and never spills off-screen. (jsdom can't lay out rects, so drive the math directly.)
  widget._setAnchorFromRect({ left: 964, right: 1020, top: 704, bottom: 760, width: 56, height: 56 });
  assert.equal(widget._anchor.h, "right", "bottom-right drop should anchor on the right");
  assert.equal(widget._anchor.v, "bottom", "bottom-right drop should anchor on the bottom");
  assert.ok(widget.root.style.right !== "auto" && widget.root.style.bottom !== "auto", "corner edges not set");
  assert.equal(widget.root.style.left, "auto", "left should be released for a right anchor");
  assert.equal(widget.root.style.top, "auto", "top should be released for a bottom anchor");

  console.log("OK — widget mounts, streams, renders markdown, resets, attaches files, selects modes, manages plugins, drags (panel + bubble), and themes per agent.");
}

main().then(() => process.exit(0)).catch((e) => { console.error("FAIL:", e.message); process.exit(1); });
