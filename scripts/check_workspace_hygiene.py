#!/usr/bin/env python3
"""Check the private workspace against the organization contract.

Reads contracts/content-agent/workspace-organization-v1.md's rules as code:

  errors   — structural violations (unknown root entries, files loose at the workspace root,
             a projects/ entry that isn't a kebab-case slug, non-secret-shaped clutter in secrets/)
  warnings — curation debt (loose files in the media/generated inbox root instead of a project
             bucket, a project without README.md or FINAL_*, an empty pointer target)

Exit codes: 0 clean (warnings allowed), 1 errors, 2 with --strict when there are warnings.
Stdlib only. Usage: python3 scripts/check_workspace_hygiene.py [--strict] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from content_agent.workspace import WORKSPACE_DIRECTORIES  # noqa: E402
from supercmo_skills import paths  # noqa: E402

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
# the filename grammar media_stem() writes: [<label>_]<capability>_<model>_<hex8>[_<i>].<ext>
MEDIA_NAME_RE = re.compile(
    r"^(?:[a-z0-9][a-z0-9-]{0,47}_)?(?:image|video|audio)_[A-Za-z0-9.\-]+_[0-9a-f]{8}(?:_\d+)?\.[A-Za-z0-9]+$"
)
_SECRETISH = ("secret", "provider", "signed-url", "signed_url", ".env")


def check(workspace: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    allowed_root = set(WORKSPACE_DIRECTORIES) | {".gitignore", "workspace.yaml"}  # workspace.yaml = the schema header

    for entry in sorted(workspace.iterdir()):
        if entry.name not in allowed_root:
            errors.append(f"unexpected workspace-root entry: {entry.name} "
                          f"(allowed: {', '.join(sorted(allowed_root))})")

    inbox = workspace / "media" / "generated"
    if inbox.is_dir():
        for entry in sorted(inbox.iterdir()):
            if entry.is_dir():
                if not SLUG_RE.match(entry.name):
                    errors.append(f"media/generated/{entry.name}/ is not a project-slug bucket")
                continue
            if entry.name == ".keep":
                continue
            if not MEDIA_NAME_RE.match(entry.name):
                warnings.append(f"inbox file doesn't match the naming grammar: media/generated/{entry.name}")
            else:
                warnings.append(f"loose file in the shared inbox (file it under a project or archive it): "
                                f"media/generated/{entry.name}")

    projects = workspace / "projects"
    if projects.is_dir():
        for entry in sorted(projects.iterdir()):
            if entry.name in {".keep", ".active-project.json"}:
                continue
            if entry.is_file():
                errors.append(f"stray file at projects/ root: {entry.name}")
                continue
            if not SLUG_RE.match(entry.name):
                errors.append(f"projects/{entry.name}/ is not a kebab-case slug")
                continue
            if not (entry / "README.md").is_file():
                warnings.append(f"projects/{entry.name}/ has no README.md (what shipped, and how?)")
            if not any(entry.glob("FINAL_*")):
                warnings.append(f"projects/{entry.name}/ has no FINAL_* deliverable yet")

    active = paths.active_project()
    if active and not (projects / active).is_dir():
        errors.append(f"active project pointer names a missing folder: projects/{active}/")

    secrets = workspace / "secrets"
    if secrets.is_dir():
        for entry in sorted(secrets.rglob("*")):
            if entry.is_file() and not any(f in entry.name.lower() for f in _SECRETISH):
                errors.append(f"non-secret-shaped file in secrets/: {entry.relative_to(workspace)}")

    return errors, warnings


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true", help="warnings also fail (exit 2)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--workspace", default=None, help="workspace path (default: discover from cwd)")
    a = ap.parse_args()

    if a.workspace:
        workspace = Path(a.workspace).resolve()
    else:
        layout = paths.content_agent_layout()
        if layout is None:
            print("❌ no content-agent workspace found from cwd", file=sys.stderr)
            sys.exit(1)
        workspace = layout.workspace
    if not workspace.is_dir():
        print(f"❌ not a directory: {workspace}", file=sys.stderr)
        sys.exit(1)

    errors, warnings = check(workspace)
    if a.json:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings,
                          "active_project": paths.active_project()}, indent=1))
    else:
        for e in errors:
            print(f"❌ {e}")
        for w in warnings:
            print(f"⚠️  {w}")
        if not errors and not warnings:
            print("✓ workspace organization clean.")
        elif not errors:
            print(f"✓ no contract violations · {len(warnings)} curation warning(s).")
    sys.exit(1 if errors else (2 if a.strict and warnings else 0))


if __name__ == "__main__":
    main()
