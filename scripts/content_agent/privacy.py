"""Public/private repository boundary checks for Content Agent."""

from pathlib import Path, PureWindowsPath
import subprocess

from content_agent.layout import ContentAgentLayout, PrivacyBoundaryError


# Traversal inventory: packaging is the only repository-wide copy surface.
# quick_validate, sync_skills, and listing_gate walk fixed public subtrees;
# check_shared_client walks fixed skills/, mcp-server/, and scripts/ roots.
GENERATED_TREE_NAMES = frozenset(
    {
        ".claude",
        ".gstack",
        ".skill-evals",
        ".supercmo",
        "_signoff-media",
        "dist",
        "htmlcov",
        "review-candidates",
        "supercmo-media",
    }
)


def validate_outer_isolation(layout: ContentAgentLayout) -> list[str]:
    """Return deterministic errors when the outer Git boundary is unsafe."""
    root = layout.root.expanduser().resolve()
    workspace = layout.workspace.expanduser().resolve()
    errors: list[str] = []

    try:
        workspace_relative = workspace.relative_to(root)
    except ValueError:
        return ["workspace isolation: workspace escapes repository root"]

    if not workspace_relative.parts or workspace_relative.parts[0] != "workspace":
        errors.append("workspace isolation: workspace must be rooted at workspace/")
    elif len(workspace_relative.parts) > 1:
        errors.append("workspace isolation: workspace must not be nested below workspace/")

    generated_parent = next(
        (
            part
            for part in workspace_relative.parts[:-1]
            if part in GENERATED_TREE_NAMES
        ),
        None,
    )
    if generated_parent is not None:
        errors.append(
            "workspace isolation: workspace must not be inside generated tree: "
            f"{generated_parent}"
        )

    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "workspace/private-canary.txt"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if ignored.returncode == 1:
        errors.append(
            "workspace isolation: workspace/private-canary.txt is not ignored"
        )
    elif ignored.returncode != 0:
        errors.append("workspace isolation: unable to check Git ignore policy")

    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if tracked.returncode != 0:
        errors.append("workspace isolation: unable to list tracked paths")
    else:
        errors.extend(
            f"workspace isolation: tracked private path: {path}"
            for path in sorted(tracked.stdout.splitlines())
            if path.startswith("workspace/")
        )
    return errors


def validate_package_entries(entries: tuple[str, ...]) -> list[str]:
    """Return errors for package roots that could reach private source."""
    errors: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry:
            errors.append("package entry must be a non-empty relative path")
            continue
        native_path = Path(entry)
        windows_path = PureWindowsPath(entry)
        if native_path.is_absolute() or windows_path.is_absolute():
            errors.append(f"package entry must not be absolute: {entry}")
        elif ".." in native_path.parts or ".." in windows_path.parts:
            errors.append(f"package entry must not traverse parent directories: {entry}")
        elif native_path.parts and native_path.parts[0] == "workspace":
            errors.append(f"package entry must not include workspace: {entry}")
    return errors


def assert_public_source(
    path: Path, layout: ContentAgentLayout, operation: str
) -> Path:
    """Resolve a source path and reject anything inside the private workspace."""
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(layout.workspace.expanduser().resolve())
    except ValueError:
        return resolved
    raise PrivacyBoundaryError(f"{operation} must not traverse private workspace: {resolved}")
