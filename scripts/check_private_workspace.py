#!/usr/bin/env python3
"""Validate the root-anchored private workspace boundary without discovery."""

from pathlib import Path
import sys

from content_agent.layout import ContentAgentLayout
from content_agent.privacy import validate_outer_isolation


def validate_private_workspace(repo_root: Path) -> list[str]:
    """Validate the configured root/workspace boundary before runtime activation."""
    root = repo_root.expanduser().resolve()
    layout = ContentAgentLayout(root=root, workspace=(root / "workspace").resolve())
    return validate_outer_isolation(layout)


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    errors = validate_private_workspace(repo_root)
    if errors:
        for error in errors:
            print(f"❌ {error}")
        sys.exit(1)
    print("✓ Private workspace isolation passed.")


if __name__ == "__main__":
    main()
