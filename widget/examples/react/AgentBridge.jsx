// React embed for the AgentBridge widget.
//
// The widget is plain DOM driven by a <script> tag, so the React integration is just a
// one-shot loader that injects the script once on mount. Nothing else is needed — the
// widget manages its own Shadow-DOM UI and WebSocket connection.
//
// Usage: render <AgentBridge /> once near the app root.
import { useEffect } from "react";

const SCRIPT_SRC =
  process.env.REACT_APP_AGENTBRIDGE_SCRIPT || "http://localhost:8000/widget/agentbridge-widget.js";
const SERVER =
  process.env.REACT_APP_AGENTBRIDGE_SERVER || "ws://localhost:8000/ws";

export default function AgentBridge({ position = "bottom-right" }) {
  useEffect(() => {
    if (window.AgentBridge?._instance) return; // already initialised
    const script = document.createElement("script");
    script.src = SCRIPT_SRC;
    script.async = true;
    script.onload = () => window.AgentBridge?.init({ server: SERVER, position });
    document.body.appendChild(script);
    // Intentionally not removed on unmount: the widget is a singleton overlay.
  }, [position]);

  return null;
}
