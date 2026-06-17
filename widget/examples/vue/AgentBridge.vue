<!--
  Vue 3 embed for the AgentBridge widget.
  Drop <AgentBridge /> once near your app root (e.g. in App.vue). Renders nothing itself —
  it just loads the widget script, which manages its own Shadow-DOM overlay + WebSocket.
-->
<script setup>
import { onMounted } from "vue";

const props = defineProps({
  position: { type: String, default: "bottom-right" },
  scriptSrc: { type: String, default: "http://localhost:8000/widget/agentbridge-widget.js" },
  server: { type: String, default: "ws://localhost:8000/ws" },
});

onMounted(() => {
  if (window.AgentBridge?._instance) return;
  const script = document.createElement("script");
  script.src = props.scriptSrc;
  script.async = true;
  script.onload = () => window.AgentBridge?.init({ server: props.server, position: props.position });
  document.body.appendChild(script);
});
</script>

<template><!-- widget renders into its own Shadow DOM overlay --></template>
