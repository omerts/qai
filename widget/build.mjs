/*
 * Build the single-file widget bundle with esbuild.
 *
 * Replaces the old CSS-inlining build.py: the widget now bundles React + assistant-ui, so it
 * needs a real bundler. The CSS is imported as a string (text loader) and injected into the
 * Shadow DOM at runtime, exactly as before — the output is still ONE self-contained IIFE file
 * embeddable via a single <script> tag, served by the backend at /widget/agentbridge-widget.js.
 *
 *   npm run build      # production (minified)
 *   npm run watch      # rebuild on change (unminified)
 */

import * as esbuild from "esbuild";
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";

const watch = process.argv.includes("--watch");

// A build stamp so a page can report exactly which widget build it's running (window.AgentBridge
// .version, the console log on mount, and the host's data-ab-version attribute). Combines the
// package version with the short git sha (+ "-dirty"), and is robust if git isn't available.
function widgetVersion() {
  let pkg = "0.0.0";
  try { pkg = JSON.parse(readFileSync(new URL("./package.json", import.meta.url))).version || pkg; } catch {}
  let sha = "nogit";
  try {
    sha = execSync("git rev-parse --short HEAD", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim();
    // Exclude the build output itself — a freshly regenerated dist shouldn't read as "dirty".
    const dirty = execSync("git status --porcelain -- . ':(exclude)dist'", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim();
    if (dirty) sha += "-dirty";
  } catch {}
  return `${pkg}+${sha}`;
}

const VERSION = widgetVersion();

const options = {
  entryPoints: ["src/agentbridge-widget.js"],
  bundle: true,
  format: "iife",
  outfile: "dist/agentbridge-widget.js",
  platform: "browser",
  target: ["es2019"],
  jsx: "automatic",
  loader: { ".css": "text" },
  define: {
    "process.env.NODE_ENV": watch ? '"development"' : '"production"',
    "__AB_WIDGET_VERSION__": JSON.stringify(VERSION),
  },
  minify: !watch,
  sourcemap: watch,
  legalComments: "none",
  logLevel: "info",
};

if (watch) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log("watching widget/src → dist/agentbridge-widget.js …");
} else {
  await esbuild.build(options);
  console.log(`built widget ${VERSION}`);
}
