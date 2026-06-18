/*
 * React + assistant-ui rendering for the AgentBridge message thread.
 *
 * The vanilla widget (agentbridge-widget.js) owns the shell, the WebSocket, and all the
 * controls (agent picker, branch/PR, inspector, drawer, composer). This module owns ONLY
 * the message list: it mounts an assistant-ui <Thread> inside the widget's Shadow DOM,
 * fed by an "external store" the vanilla code writes into as protocol messages arrive.
 *
 * The bridge is the seam between the two worlds:
 *   - vanilla side calls bridge.addUser / chunk / addSystem / reset / setRunning …
 *   - React side subscribes to it via useSyncExternalStore and hands the messages to
 *     assistant-ui's useExternalStoreRuntime, which renders + streams them.
 *
 * assistant-ui's headless *primitives* are used (not its Tailwind-styled components) so the
 * markup is plain DOM we can style with the widget's own CSS — keeping Shadow DOM isolation.
 */

import React, { useSyncExternalStore } from "react";
import { createRoot } from "react-dom/client";
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  ThreadPrimitive,
  MessagePrimitive,
  MessagePartPrimitive,
  useMessage,
} from "@assistant-ui/react";
import { MarkdownTextPrimitive } from "@assistant-ui/react-markdown";

let _seq = 0;
function uid() { return "ab" + ++_seq; }

// --------------------------------------------------------------------------- //
// External store — plain JS, shared with the vanilla widget.
// --------------------------------------------------------------------------- //

export function createThreadBridge() {
  // `messages` is replaced (never mutated) on every change so useSyncExternalStore's
  // snapshot comparison sees a new reference and React re-renders.
  var messages = [];
  var running = false;
  var runLabel = "";  // display label of the agent currently working ("Claude Code", …)
  var emptyHint = "";
  var current = null; // id of the assistant/stderr message currently streaming
  var listeners = new Set();
  var submitHandler = null;

  function emit() { listeners.forEach(function (l) { l(); }); }

  return {
    // ---- React-facing (read) ----
    subscribe: function (l) { listeners.add(l); return function () { listeners.delete(l); }; },
    getMessages: function () { return messages; },
    getRunning: function () { return running; },
    getRunLabel: function () { return runLabel; },
    getEmptyHint: function () { return emptyHint; },
    // ---- composer hook (unused while the vanilla composer is active, but kept so
    // assistant-ui's onNew has somewhere to go if its composer is ever enabled) ----
    onSubmit: function (fn) { submitHandler = fn; },
    submit: function (text) { if (submitHandler) submitHandler(text); },

    // ---- vanilla-facing (write) ----
    reset: function () { messages = []; running = false; current = null; emit(); },
    setEmptyHint: function (t) { emptyHint = t || ""; emit(); },
    setRunning: function (b, label) {
      running = !!b;
      if (label !== undefined) runLabel = label || "";
      emit();
    },

    addUser: function (text) {
      current = null;
      messages = messages.concat({ id: uid(), role: "user", text: text });
      emit();
    },
    // A complete (non-streamed) assistant message — used when replaying history.
    addAgent: function (text) {
      current = null;
      messages = messages.concat({ id: uid(), role: "assistant", stream: "stdout", text: text });
      emit();
    },
    addSystem: function (text, variant) {
      current = null;
      messages = messages.concat({ id: uid(), role: "system", variant: variant || "note", text: text });
      emit();
    },
    // Streamed agent output. stdout/thinking render as the assistant; stderr as a
    // system "error-ish" note. Consecutive chunks of the same stream append in place.
    chunk: function (text, stream) {
      stream = stream || "stdout";
      var role = stream === "stderr" ? "system" : "assistant";
      var variant = stream === "stderr" ? "stderr" : null;
      var cur = current != null ? messages.find(function (m) { return m.id === current; }) : null;
      if (!cur || cur.stream !== stream) {
        cur = { id: uid(), role: role, stream: stream, variant: variant, text: "" };
        current = cur.id;
        messages = messages.concat(cur);
      }
      messages = messages.map(function (m) {
        return m.id === current ? Object.assign({}, m, { text: m.text + text }) : m;
      });
      emit();
    },
  };
}

// --------------------------------------------------------------------------- //
// Message conversion: our store shape -> assistant-ui ThreadMessageLike.
// --------------------------------------------------------------------------- //

function convertMessage(m) {
  if (m.role === "user") return { role: "user", content: m.text };
  if (m.role === "system") {
    return { role: "system", content: m.text, metadata: { custom: { variant: m.variant || "note" } } };
  }
  // assistant
  if (m.stream === "thinking") {
    return { role: "assistant", content: [{ type: "reasoning", text: m.text }] };
  }
  return { role: "assistant", content: m.text };
}

// --------------------------------------------------------------------------- //
// Rendering components (headless primitives + widget CSS classes).
// --------------------------------------------------------------------------- //

function PlainText() {
  return <MessagePartPrimitive.Text />;
}

function MarkdownText() {
  return <MarkdownTextPrimitive />;
}

function ReasoningText() {
  return (
    <div className="ab-reasoning">
      <MessagePartPrimitive.Text />
    </div>
  );
}

function UserMessage() {
  return (
    <MessagePrimitive.Root className="ab-msg user ab-md">
      <MessagePrimitive.Parts components={{ Text: MarkdownText }} />
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="ab-msg agent ab-md">
      <MessagePrimitive.Parts components={{ Text: MarkdownText, Reasoning: ReasoningText }} />
    </MessagePrimitive.Root>
  );
}

function SystemMessage() {
  var msg = useMessage();
  var variant = msg && msg.metadata && msg.metadata.custom && msg.metadata.custom.variant;
  var cls =
    variant === "error" ? "ab-msg error" :
    variant === "stderr" ? "ab-msg agent stderr" :
    "ab-msg system";
  return (
    <MessagePrimitive.Root className={cls}>
      <MessagePrimitive.Parts components={{ Text: PlainText }} />
    </MessagePrimitive.Root>
  );
}

function EmptyState(props) {
  var hint = useSyncExternalStore(props.bridge.subscribe, props.bridge.getEmptyHint, props.bridge.getEmptyHint);
  return <div className="ab-empty">{hint || "Pick an agent above and press + to start a chat."}</div>;
}

// Animated "thinking" bubble shown while the agent is working and hasn't started its reply.
function TypingIndicator(props) {
  var who = props.label ? props.label : "Assistant";
  return (
    <div className="ab-msg agent ab-typing" role="status" aria-live="polite">
      <span className="ab-typing-label">{who + " is thinking"}</span>
      <span className="ab-typing-dots" aria-hidden="true">
        <span></span><span></span><span></span>
      </span>
    </div>
  );
}

var MESSAGE_COMPONENTS = {
  UserMessage: UserMessage,
  AssistantMessage: AssistantMessage,
  SystemMessage: SystemMessage,
};

function ThreadView(props) {
  var bridge = props.bridge;
  var messages = useSyncExternalStore(bridge.subscribe, bridge.getMessages, bridge.getMessages);
  var running = useSyncExternalStore(bridge.subscribe, bridge.getRunning, bridge.getRunning);
  var runLabel = useSyncExternalStore(bridge.subscribe, bridge.getRunLabel, bridge.getRunLabel);

  // Show the thinking bubble while working and the agent hasn't begun streaming its reply
  // (i.e. the last entry is the user's turn or a system note) — the streaming text is its
  // own feedback once it starts.
  var last = messages.length ? messages[messages.length - 1] : null;
  var thinking = running && (!last || last.role !== "assistant");

  var runtime = useExternalStoreRuntime({
    messages: messages,
    isRunning: running,
    convertMessage: convertMessage,
    onNew: function (message) {
      var text = (message.content || [])
        .filter(function (p) { return p.type === "text"; })
        .map(function (p) { return p.text; })
        .join("");
      bridge.submit(text);
      return Promise.resolve();
    },
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="ab-thread">
        <ThreadPrimitive.Viewport className="ab-thread-viewport">
          <ThreadPrimitive.Empty>
            <EmptyState bridge={bridge} />
          </ThreadPrimitive.Empty>
          <ThreadPrimitive.Messages components={MESSAGE_COMPONENTS} />
          {thinking ? <TypingIndicator label={runLabel} /> : null}
        </ThreadPrimitive.Viewport>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}

// Mount the thread into `container` (a node already attached inside the Shadow DOM).
export function mountThread(container, bridge) {
  var root = createRoot(container);
  root.render(<ThreadView bridge={bridge} />);
  return root;
}
