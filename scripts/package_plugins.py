#!/usr/bin/env python3
"""Package the repo-as-one-plugin into a distributable plugin bundle.

Repo-as-one-plugin: the whole repo IS the plugin. Native install
(`/plugin marketplace add ...`) needs no compiler — this bundle is the additive
delivery artifact: what a user uploads via Claude Cowork / desktop
("Settings -> Plugins -> Upload local plugin", a `.zip`), and what `release.yml`
attaches to each GitHub Release.

It bundles every runtime plugin component and excludes dev-only files. It also
excludes the top-level `bin/` (the npx installer) and `package.json`:
claude.ai-hosted plugins forbid a top-level `bin/` (executables on PATH with no
approval surface), so the uploaded bundle must not contain it.
"""
import os
import sys
import json
import shutil
import zipfile
import tempfile
from pathlib import Path

from content_agent.layout import ContentAgentLayout
from content_agent.privacy import (
    assert_public_source,
    validate_outer_isolation,
    validate_package_entries,
)

# Top-level entries that make up the installable plugin — everything the plugin
# needs at runtime, and NOT bin/ or package.json (see the module docstring).
PLUGIN_ENTRIES = (
    ".claude-plugin",   # plugin.json / marketplace.json / supercmo.json
    "mcp-server",       # the MCP server the skills call
    "skills",           # the skills
    "scripts",          # the engine the MCP server imports (supercmo_skills, etc.)
    "LICENSE",          # Apache-2.0 (redistribution)
    "NOTICE",           # Apache-2.0 NOTICE (redistribution)
)

# Dev-only / non-runtime files excluded from the shipped bundle.
EXCLUDE_PATTERNS = shutil.ignore_patterns(
    "evals", "__pycache__", "*.pyc", "*.pyo", "*.pyd", "*.egg-info",
    ".DS_Store", "node_modules", ".git", ".pytest_cache", ".benchmarks",
)


def plugin_name(repo_root):
    """Read the plugin name from the root plugin.json (fallback: 'supercmo')."""
    try:
        with open(os.path.join(repo_root, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            return json.load(f).get("name", "supercmo")
    except Exception:
        return "supercmo"


def package(repo_root):
    repo_root = Path(repo_root).resolve()
    layout = ContentAgentLayout(
        root=repo_root, workspace=(repo_root / "workspace").resolve()
    )
    entry_errors = validate_package_entries(PLUGIN_ENTRIES)
    isolation_errors = validate_outer_isolation(layout)
    errors = entry_errors + isolation_errors
    if errors:
        raise RuntimeError("workspace isolation failed: " + "; ".join(errors))

    validated_sources = []
    missing_entries = []
    for entry in PLUGIN_ENTRIES:
        source_entry = repo_root / entry
        if source_entry.is_symlink():
            raise RuntimeError(f"package source must not be a symlink: {entry}")
        src = assert_public_source(source_entry, layout, "package source")
        if not src.exists():
            missing_entries.append(entry)
            continue
        if src.is_dir():
            symlink = next((path for path in src.rglob("*") if path.is_symlink()), None)
            if symlink is not None:
                raise RuntimeError(
                    "package source must not contain symlink: "
                    f"{symlink.relative_to(repo_root)}"
                )
        validated_sources.append((entry, src))

    name = plugin_name(repo_root)
    print(f"--> Packaging repo-as-one-plugin: {name}")
    for entry in missing_entries:
        print(f"⚠️  missing plugin entry (skipped): {entry}")

    dist_dir = os.path.join(repo_root, "dist")
    os.makedirs(dist_dir, exist_ok=True)
    bundle = os.path.join(dist_dir, f"{name}-plugin.zip")
    if os.path.exists(bundle):
        os.remove(bundle)

    with tempfile.TemporaryDirectory() as staging:
        for entry, src in validated_sources:
            dest = os.path.join(staging, entry)
            if src.is_dir():
                shutil.copytree(src, dest, ignore=EXCLUDE_PATTERNS)
            else:
                shutil.copy2(src, dest)

        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, files in os.walk(staging):
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    zf.write(full, arcname=os.path.relpath(full, staging))

    print(f"✓ Packaged '{name}' → {bundle} ({os.path.getsize(bundle)} bytes).")
    return True


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.append(os.path.join(repo_root, "scripts"))
    import quick_validate
    print("=== Pre-package validation ===")
    if not (quick_validate.validate_skills(repo_root) and quick_validate.validate_plugin(repo_root)):
        print("❌ Validation failed. Aborting.")
        sys.exit(1)
    if not package(repo_root):
        sys.exit(1)
    print("\n✓ Package complete.")
    sys.exit(0)


if __name__ == "__main__":
    main()
