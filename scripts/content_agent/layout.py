"""Discover and enforce Content Agent's private workspace boundary."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any


CONFIG_NAME = "content-agent.config.json"
CANONICAL_ROOT_NAME = "content-agent"
SUPPORTED_WORKSPACE_SCHEMA_MIN = 1
SUPPORTED_WORKSPACE_SCHEMA_MAX = 1

_CONFIG_FIELDS = frozenset({"schema_version", "canonical_root_name", "workspace"})
_WORKSPACE_HEADER_FIELDS = frozenset(
    {"schema_version", "workspace_id", "created_at", "display_name"}
)
_WORKSPACE_ID_PATTERN = re.compile(r"^ws_[0-9a-f]{32}$")
_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class ContentAgentLayoutError(RuntimeError):
    """The Content Agent filesystem contract is invalid or cannot be found."""


class ContentAgentMarkerInactive(ContentAgentLayoutError):
    """A valid marker exists, but the checkout has not yet been renamed."""


class PrivacyBoundaryError(ContentAgentLayoutError):
    """A path attempts to leave the private workspace."""


class UnsupportedWorkspaceSchemaError(ContentAgentLayoutError):
    """A workspace header uses an unsupported schema version."""


@dataclass(frozen=True)
class WorkspaceHeader:
    schema_version: int
    workspace_id: str
    created_at: str
    display_name: str | None = None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_json_object(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContentAgentLayoutError(f"invalid {description}: {path}") from error
    if not isinstance(value, dict):
        raise ContentAgentLayoutError(f"{description} must be a JSON object: {path}")
    return value


def _require_exact_fields(
    value: dict[str, Any], expected_fields: frozenset[str], description: str
) -> None:
    if set(value) != expected_fields:
        raise ContentAgentLayoutError(f"{description} has missing or unexpected fields")


def _is_within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _validate_workspace_value(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ContentAgentLayoutError("workspace must be a non-empty relative path")
    native_path = Path(value)
    windows_path = PureWindowsPath(value)
    if native_path.is_absolute() or windows_path.is_absolute():
        raise ContentAgentLayoutError("workspace must be a relative path")
    if ".." in native_path.parts or ".." in windows_path.parts:
        raise ContentAgentLayoutError("workspace must not traverse parent directories")
    if value in {".", "./"}:
        raise ContentAgentLayoutError("workspace must be below the repository root")
    return value


def _validate_config(config: dict[str, Any]) -> str:
    _require_exact_fields(config, _CONFIG_FIELDS, "content-agent config")
    if not _is_int(config["schema_version"]) or config["schema_version"] != 1:
        raise ContentAgentLayoutError("unsupported content-agent config schema")
    if (
        not isinstance(config["canonical_root_name"], str)
        or config["canonical_root_name"] != CANONICAL_ROOT_NAME
    ):
        raise ContentAgentLayoutError("invalid content-agent canonical root name")
    return _validate_workspace_value(config["workspace"])


@dataclass(frozen=True)
class ContentAgentLayout:
    root: Path
    workspace: Path

    @classmethod
    def discover(cls, start: Path) -> "ContentAgentLayout":
        current = start.expanduser().resolve()
        if current.is_file():
            current = current.parent
        for candidate in (current, *current.parents):
            config_path = candidate / CONFIG_NAME
            if not config_path.is_file():
                continue
            workspace_value = _validate_config(
                _read_json_object(config_path, "content-agent config")
            )
            if candidate.name != CANONICAL_ROOT_NAME:
                raise ContentAgentMarkerInactive("content-agent marker is not active")
            root = candidate.resolve()
            workspace = (root / workspace_value).resolve()
            if not _is_within(workspace, root) or workspace == root:
                raise ContentAgentLayoutError("workspace escapes repository root")
            return cls(root=root, workspace=workspace)
        raise ContentAgentLayoutError(f"{CONFIG_NAME} not found from {start}")

    def require_private_path(self, path: Path, purpose: str) -> Path:
        resolved = path.expanduser().resolve()
        if not _is_within(resolved, self.workspace):
            raise PrivacyBoundaryError(
                f"{purpose} escapes private workspace: {resolved}"
            )
        return resolved


def _is_timestamp(value: str) -> bool:
    if not _TIMESTAMP_PATTERN.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def read_workspace_header(path: Path) -> WorkspaceHeader:
    """Read a JSON-compatible YAML workspace header without modifying it."""
    header = _read_json_object(path, "workspace header")
    if not {"schema_version", "workspace_id", "created_at"}.issubset(header):
        raise ContentAgentLayoutError("workspace header has missing fields")
    if not _is_int(header["schema_version"]):
        raise ContentAgentLayoutError("workspace schema_version must be an integer")
    if not (
        SUPPORTED_WORKSPACE_SCHEMA_MIN
        <= header["schema_version"]
        <= SUPPORTED_WORKSPACE_SCHEMA_MAX
    ):
        raise UnsupportedWorkspaceSchemaError("unsupported workspace schema version")
    if not set(header).issubset(_WORKSPACE_HEADER_FIELDS):
        raise ContentAgentLayoutError("workspace header has unexpected fields")
    if not isinstance(header["workspace_id"], str) or not _WORKSPACE_ID_PATTERN.fullmatch(
        header["workspace_id"]
    ):
        raise ContentAgentLayoutError("workspace_id must match ws_<32 lowercase hex characters>")
    if not isinstance(header["created_at"], str) or not _is_timestamp(header["created_at"]):
        raise ContentAgentLayoutError("created_at must be an ISO 8601 timestamp with timezone")
    display_name = header.get("display_name")
    if "display_name" in header and not isinstance(display_name, str):
        raise ContentAgentLayoutError("display_name must be a string")
    return WorkspaceHeader(
        schema_version=header["schema_version"],
        workspace_id=header["workspace_id"],
        created_at=header["created_at"],
        display_name=display_name,
    )
