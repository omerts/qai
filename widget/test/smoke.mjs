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
