"""Canonical filesystem locations for generated media and runtime files."""
import os
import tempfile
from pathlib import Path

from content_agent.layout import (
    CONFIG_NAME,
    ContentAgentLayout,
    ContentAgentLayoutError,
    ContentAgentMarkerInactive,
)

OUTPUT_DIR_ENV = "SUPERCMO_OUTPUT_DIR"
SCRATCH_DIR_ENV = "SUPERCMO_SCRATCH_DIR"
CACHE_DIR_ENV = "SUPERCMO_CACHE_DIR"
PROJECTION_DIR_ENV = "SUPERCMO_PROJECTION_DIR"

_OUTPUT_DEFAULT = "./supercmo-media"
_SCRATCH_DEFAULT = Path(tempfile.gettempdir()) / "supercmo-work"
_CACHE_DEFAULT = Path(tempfile.gettempdir()) / "supercmo-cache"
_PROJECTION_DEFAULT = ".supercmo/projections"


def content_agent_layout(start: Path | None = None) -> ContentAgentLayout | None:
    """Return the active private layout, or ``None`` outside a Content Agent checkout."""
    location = start or Path.cwd()
    try:
        return ContentAgentLayout.discover(location)
    except ContentAgentMarkerInactive:
        return None
    except ContentAgentLayoutError as error:
        if str(error) == f"{CONFIG_NAME} not found from {location}":
            return None
        raise


def _destination(
    explicit: str | None,
    environment: str,
    public_default: str | Path,
    private_default: str,
    purpose: str,
) -> str:
    candidate = explicit or os.environ.get(environment) or public_default
    layout = content_agent_layout()
    if layout is None:
        return os.path.abspath(os.path.expanduser(candidate))
    private_candidate = (
        explicit or os.environ.get(environment) or layout.workspace / private_default
    )
    return str(layout.require_private_path(Path(private_candidate), purpose))


def output_dir(explicit: str | None = None) -> str:
    """Where durable generated media lands: explicit arg > $SUPERCMO_OUTPUT_DIR > ./supercmo-media."""
    return _destination(
        explicit,
        OUTPUT_DIR_ENV,
        _OUTPUT_DEFAULT,
        "media/generated",
        "output",
    )


def scratch_dir(explicit: str | None = None) -> str:
    """Where temporary product and media work files land."""
    return _destination(
        explicit,
        SCRATCH_DIR_ENV,
        _SCRATCH_DEFAULT,
        "cache/scratch",
        "scratch",
    )


def cache_dir(explicit: str | None = None) -> str:
    """Where runtime cache files land."""
    return _destination(
        explicit,
        CACHE_DIR_ENV,
        _CACHE_DEFAULT,
        "cache/runtime",
        "cache",
    )


def projection_dir(explicit: str | None = None) -> str:
    """Where generated projections land."""
    return _destination(
        explicit,
        PROJECTION_DIR_ENV,
        _PROJECTION_DEFAULT,
        "projections/generated",
        "projection",
    )
