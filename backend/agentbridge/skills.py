"""Enumerate the Agent Skills available to the agent, for the widget's "/" menu.

Skills are ``.claude/skills/<name>/SKILL.md`` files. We surface the workspace's skills (which
AgentBridge also copies into each chat's worktree) plus the user's personal skills, parsing the
``name``/``description`` from each SKILL.md's YAML frontmatter so the widget can show + filter them.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _parse_skill(skill_md: Path) -> dict | None:
    try:
        text = skill_md.read_text(errors="replace")
    except OSError:
        return None
    name = skill_md.parent.name
    description = ""
    m = _FRONTMATTER_RE.match(text)
    if m:
        fm = m.group(1)
        nm = re.search(r"^name:\s*(.+)$", fm, re.M)
        if nm:
            name = nm.group(1).strip().strip("\"'")
        dm = re.search(r"^description:\s*(.+)$", fm, re.M)
        if dm:
            description = dm.group(1).strip().strip("\"'")
    name = name.strip()
    return {"name": name, "description": description} if name else None


def list_skills(workspace: Path) -> list[dict]:
    """Skills available to the agent: the workspace's ``.claude/skills/`` first, then the user's
    ``~/.claude/skills/``. De-duplicated by name (workspace wins), sorted by name."""
    roots = [Path(workspace) / ".claude" / "skills"]
    home = os.environ.get("HOME")
    if home:
        roots.append(Path(home) / ".claude" / "skills")

    seen: dict[str, dict] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for skill_md in sorted(root.glob("*/SKILL.md")):
            info = _parse_skill(skill_md)
            if info and info["name"] not in seen:
                seen[info["name"]] = info
    return sorted(seen.values(), key=lambda s: s["name"].lower())
