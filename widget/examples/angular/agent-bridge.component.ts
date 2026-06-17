// Angular embed for the AgentBridge widget.
//
// Add <app-agent-bridge></app-agent-bridge> once near your app root. The component renders
// nothing — it injects the widget script on init, which manages its own Shadow-DOM overlay
// and WebSocket connection.
//
// Declare it in your module (or use standalone: true as below for Angular 15+).
import { Component, Input, OnInit } from "@angular/core";

@Component({
  selector: "app-agent-bridge",
  standalone: true,
  template: "", // widget renders into its own Shadow DOM overlay
})
export class AgentBridgeComponent implements OnInit {
  @Input() position = "bottom-right";
  @Input() scriptSrc = "http://localhost:8000/widget/agentbridge-widget.js";
  @Input() server = "ws://localhost:8000/ws";

  ngOnInit(): void {
    const w = window as any;
    if (w.AgentBridge?._instance) return;
    const script = document.createElement("script");
    script.src = this.scriptSrc;
    script.async = true;
    script.onload = () =>
      w.AgentBridge?.init({ server: this.server, position: this.position });
    document.body.appendChild(script);
  }
}
