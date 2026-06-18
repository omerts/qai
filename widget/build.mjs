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

const watch = process.argv.includes("--watch");

const options = {
  entryPoints: ["src/agentbridge-widget.js"],
  bundle: true,
  format: "iife",
  outfile: "dist/agentbridge-widget.js",
  platform: "browser",
  target: ["es2019"],
  jsx: "automatic",
  loader: { ".css": "text" },
  define: { "process.env.NODE_ENV": watch ? '"development"' : '"production"' },
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
}
