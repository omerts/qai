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

const widget = AgentBridge.init({ server: "ws://localhost:9/ws" });
assert.ok(widget && widget.shadow, "widget did not mount a shadow root");

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
  widget.queue = []; widget._renderQueue(); widget.activeChatId = null;

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

  console.log("OK — widget mounts, streams, renders markdown, resets, and themes per agent.");
}

main().then(() => process.exit(0)).catch((e) => { console.error("FAIL:", e.message); process.exit(1); });
