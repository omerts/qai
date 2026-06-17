#!/usr/bin/env python3
"""Build the single-file widget bundle: inline the CSS into the JS.

No Node/toolchain required. Produces widget/dist/agentbridge-widget.js, which is fully
self-contained and embeddable via one <script> tag.

    python widget/build.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
DIST = ROOT / "dist"


def main() -> None:
    js = (SRC / "agentbridge-widget.js").read_text()
    css = (SRC / "agentbridge-widget.css").read_text()

    # Replace the placeholder string literal with a JSON-encoded CSS string so any
    # quotes/newlines in the CSS are safely escaped.
    placeholder = '"/*__INJECT_CSS__*/"'
    if placeholder not in js:
        raise SystemExit("CSS placeholder not found in agentbridge-widget.js")
    bundled = js.replace(placeholder, json.dumps(css))

    DIST.mkdir(exist_ok=True)
    out = DIST / "agentbridge-widget.js"
    out.write_text(bundled)
    print(f"Wrote {out.relative_to(ROOT.parent)} ({len(bundled):,} bytes)")


if __name__ == "__main__":
    main()
