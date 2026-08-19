"""Initialize and protect Content Agent's private local workspace."""

import fnmatch
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from content_agent.layout import (
    ContentAgentLayout,
    ContentAgentLayoutError,
    SUPPORTED_WORKSPACE_SCHEMA_MAX,
    read_workspace_header,
)
from content_agent.privacy import validate_outer_isolation


WORKSPACE_DIRECTORIES = (
    "channels",
    "projects",
    "library",
    "evaluations",
    "migrations",
    "projections",
    "archive",
    "cache",
    "secrets",
    "media",
)
_IGNORED_DIRECTORIES = frozenset({"media", "cache", "secrets"})
_PROTECTED_DIRECTORY_NAMES = _IGNORED_DIRECTORIES | frozenset(
    {"node_modules", "__pycache__"}
)
_PROTECTED_FILE_PATTERNS = (
    ".env",
    ".env.*",
    "*.signed-url",
    "*.provider-payload.json",
    "node_modules",
    "__pycache__",
    "*.pyc",
    "*.tmp",
    "*.mp4",
    "*.mov",
    "*.mkv",
    "*.webm",
    "*.mp3",
    "*.wav",
    "*.flac",
    "*.m4a",
    "*.aiff",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.psd",
    "*.aep",
    "*.prproj",
    "*.blend",
)
_SECRET_NAME_FRAGMENTS = ("secret", "provider", "signed-url", "signed_url")
_WORKSPACE_ID_PATTERN = re.compile(r"^ws_[0-9a-f]{32}$")

INNER_IGNORE = """/media/
/cache/
/secrets/
.env
.env.*
*.signed-url
*.provider-payload.json
node_modules/
__pycache__/
*.pyc
*.tmp
*.mp4
*.mov
*.mkv
*.webm
*.mp3
*.wav
*.flac
*.m4a
*.aiff
*.png
*.jpg
*.jpeg
*.gif
*.webp
*.psd
*.aep
*.prproj
*.blend
"""


@dataclass(frozen=True)
class WorkspaceInitReceipt:
    workspace: Path
    created: bool
    schema_version: int
    directories: tuple[str, ...]


def _timestamp(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ContentAgentLayoutError("workspace initialization time must include timezone")
    return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _atomic_write_private(path: Path, contents: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(contents)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _require_outer_isolation(layout: ContentAgentLayout) -> None:
    errors = validate_outer_isolation(layout)
    if errors:
        raise ContentAgentLayoutError("; ".join(errors))


def _completed_workspace_errors(workspace: Path) -> list[str]:
    errors: list[str] = []
    header_path = workspace / "workspace.yaml"
    ignore_path = workspace / ".gitignore"

    if header_path.is_symlink():
        errors.append("workspace.yaml must not be a symlink")
    if not ignore_path.is_file() or ignore_path.is_symlink():
        errors.append(".gitignore is missing or invalid")
    else:
        try:
            ignore_contents = ignore_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(".gitignore cannot be read")
        else:
            if ignore_contents != INNER_IGNORE:
                errors.append(".gitignore does not match the required policy")

    if os.name == "posix":
        for config_path in (header_path, ignore_path):
            if config_path.exists() and config_path.stat().st_mode & 0o777 != 0o600:
                errors.append(f"{config_path.name} permissions must be 0600")

    for directory in WORKSPACE_DIRECTORIES:
        directory_path = workspace / directory
        if not directory_path.is_dir() or directory_path.is_symlink():
            errors.append(f"required directory is missing or invalid: {directory}")
            continue
        marker = directory_path / ".keep"
        if directory in _IGNORED_DIRECTORIES:
            if marker.exists() or marker.is_symlink():
                errors.append(f"ignored directory must not contain .keep: {directory}")
        elif not marker.is_file() or marker.is_symlink():
            errors.append(f"required marker is missing or invalid: {directory}/.keep")
        else:
            try:
                marker_contents = marker.read_bytes()
            except OSError:
                errors.append(f"required marker cannot be read: {directory}/.keep")
            else:
                if marker_contents:
                    errors.append(f"required marker is altered: {directory}/.keep")
    return errors


def _headerless_workspace_conflicts(workspace: Path) -> list[str]:
    if not workspace.exists():
        return []
    if not workspace.is_dir() or workspace.is_symlink():
        return ["workspace path is not a local directory"]

    conflicts: list[str] = []
    allowed_root_names = set(WORKSPACE_DIRECTORIES) | {".gitignore"}
    try:
        root_entries = sorted(workspace.iterdir(), key=lambda entry: entry.name)
    except OSError:
        return ["workspace cannot be inspected"]
    for entry in root_entries:
        if entry.name not in allowed_root_names:
            conflicts.append(f"unexpected path: {entry.name}")

    ignore_path = workspace / ".gitignore"
    if ignore_path.exists() or ignore_path.is_symlink():
        if not ignore_path.is_file() or ignore_path.is_symlink():
            conflicts.append(".gitignore is invalid")
        else:
            try:
                ignore_contents = ignore_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                conflicts.append(".gitignore cannot be read")
            else:
                if ignore_contents != INNER_IGNORE:
                    conflicts.append(".gitignore conflicts with the required policy")
            if os.name == "posix" and ignore_path.stat().st_mode & 0o777 != 0o600:
                conflicts.append(".gitignore permissions must be 0600")

    for directory in WORKSPACE_DIRECTORIES:
        directory_path = workspace / directory
        if not directory_path.exists() and not directory_path.is_symlink():
            continue
        if not directory_path.is_dir() or directory_path.is_symlink():
            conflicts.append(f"required directory path is invalid: {directory}")
            continue
        try:
            entries = sorted(directory_path.iterdir(), key=lambda entry: entry.name)
        except OSError:
            conflicts.append(f"required directory cannot be inspected: {directory}")
            continue
        if directory in _IGNORED_DIRECTORIES:
            conflicts.extend(
                f"unexpected path: {directory}/{entry.name}" for entry in entries
            )
            continue
        for entry in entries:
            if entry.name != ".keep":
                conflicts.append(f"unexpected path: {directory}/{entry.name}")
                continue
            if not entry.is_file() or entry.is_symlink():
                conflicts.append(f"required marker is invalid: {directory}/.keep")
                continue
            try:
                marker_contents = entry.read_bytes()
            except OSError:
                conflicts.append(f"required marker cannot be read: {directory}/.keep")
            else:
                if marker_contents:
                    conflicts.append(f"required marker is altered: {directory}/.keep")
    return conflicts


def initialize_workspace(
    layout: ContentAgentLayout, workspace_id: str, now: datetime
) -> WorkspaceInitReceipt:
    """Create one ignored private workspace without touching Git configuration."""
    _require_outer_isolation(layout)
    if not isinstance(workspace_id, str) or not _WORKSPACE_ID_PATTERN.fullmatch(
        workspace_id
    ):
        raise ContentAgentLayoutError(
            "workspace_id must match ws_<32 lowercase hex characters>"
        )
    workspace = layout.workspace
    header_path = workspace / "workspace.yaml"

    if header_path.exists():
        header = read_workspace_header(header_path)
        if header.workspace_id != workspace_id:
            raise ContentAgentLayoutError("workspace is already initialized with a different workspace_id")
        completion_errors = _completed_workspace_errors(workspace)
        if completion_errors:
            raise ContentAgentLayoutError(
                "workspace initialization is incomplete or altered: "
                + "; ".join(completion_errors)
            )
        return WorkspaceInitReceipt(
            workspace=workspace,
            created=False,
            schema_version=header.schema_version,
            directories=WORKSPACE_DIRECTORIES,
        )

    partial_conflicts = _headerless_workspace_conflicts(workspace)
    if partial_conflicts:
        raise ContentAgentLayoutError(
            "headerless workspace contains conflicting data: "
            + "; ".join(partial_conflicts)
        )
    workspace.mkdir(parents=True, exist_ok=True)
    for directory in WORKSPACE_DIRECTORIES:
        directory_path = workspace / directory
        directory_path.mkdir(exist_ok=True)
        if directory not in _IGNORED_DIRECTORIES:
            _atomic_write_private(directory_path / ".keep", "")

    header = {
        "schema_version": SUPPORTED_WORKSPACE_SCHEMA_MAX,
        "workspace_id": workspace_id,
        "created_at": _timestamp(now),
    }
    _atomic_write_private(workspace / ".gitignore", INNER_IGNORE)
    _atomic_write_private(header_path, json.dumps(header, indent=2) + "\n")
    return WorkspaceInitReceipt(
        workspace=workspace,
        created=True,
        schema_version=SUPPORTED_WORKSPACE_SCHEMA_MAX,
        directories=WORKSPACE_DIRECTORIES,
    )


def _protected_staged_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    if any(part in _PROTECTED_DIRECTORY_NAMES for part in candidate.parts[:-1]):
        return True
    filename = candidate.name.lower()
    return (
        any(fnmatch.fnmatchcase(filename, pattern) for pattern in _PROTECTED_FILE_PATTERNS)
        or any(fragment in filename for fragment in _SECRET_NAME_FRAGMENTS)
    )


def validate_inner_staging(workspace: Path) -> list[str]:
    """Return deterministic errors for sensitive paths staged in the inner repository."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=workspace,
        text=False,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return ["inner staging: unable to inspect Git staged paths"]
    paths = sorted(
        path.decode("utf-8", "surrogateescape")
        for path in result.stdout.split(b"\0")
        if path
    )
    return [
        f"inner staging: protected staged path: {path}"
        for path in paths
        if _protected_staged_path(path)
    ]
