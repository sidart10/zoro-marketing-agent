"""Workspace project routing — thin MCP binding over supercmo_skills.paths.

Sets/reads the active project so every generation lands in media/generated/<slug>/ instead of one
flat inbox, per contracts/content-agent/workspace-organization-v1.md.
"""
import os
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "scripts"))

import registry  # noqa: E402
from supercmo_skills import paths  # noqa: E402


WORKSPACE_PROJECT = {
    "name": "workspace_project",
    "description": (
        "Manage the ACTIVE PROJECT that routes generated media. Call `set` with a kebab-case slug "
        "at the START of any campaign/session (e.g. 'riwayat-rida-suit-set'): from then on every "
        "image_generate / video_generate / audio_generate output lands in "
        "workspace/media/generated/<slug>/ instead of the shared inbox, and the project folder "
        "workspace/projects/<slug>/ is created for curated deliverables (FINAL_*, README.md, "
        "product-facts.md). `status` shows the active slug and resolved output dir; `list` shows "
        "all projects and whether each has a README and FINAL; `clear` reverts to the bare inbox. "
        "Naming/lifecycle contract: contracts/content-agent/workspace-organization-v1.md."
    ),
    "inputSchema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": ["status", "set", "clear", "list"]},
            "slug": {"type": "string",
                     "description": "Project slug for `set` — kebab-case, e.g. 'riwayat-rida-suit-set'."},
        },
    },
}


def workspace_project(args):
    action = args.get("action")
    layout = paths.content_agent_layout()
    if layout is None:
        return {"ok": False, "error": "no active content-agent workspace here."}
    if action == "set":
        return paths.set_active_project(args.get("slug"))
    if action == "clear":
        return paths.clear_active_project()
    if action == "list":
        projects_dir = layout.workspace / "projects"
        rows = []
        for entry in sorted(projects_dir.iterdir()) if projects_dir.is_dir() else []:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            rows.append({
                "slug": entry.name,
                "valid_slug": bool(paths.PROJECT_SLUG_RE.match(entry.name)),
                "has_readme": (entry / "README.md").is_file(),
                "has_final": any(entry.glob("FINAL_*")),
            })
        return {"ok": True, "active": paths.active_project(), "projects": rows}
    if action == "status":
        return {"ok": True, "active": paths.active_project(), "output_dir": paths.output_dir(),
                "hint": "outputs land in output_dir; curated deliverables go to workspace/projects/<slug>/"}
    return {"ok": False, "error": f"unknown action: {action!r}"}


registry.register(WORKSPACE_PROJECT, workspace_project)
