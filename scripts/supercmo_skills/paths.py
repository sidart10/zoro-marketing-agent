"""Canonical filesystem locations for generated media and runtime files."""
import json
import os
import re
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


# ---------------------------------------------------------------- active project
# The inbox contract: generations land under media/generated/<active-project-slug>/ so one flat
# folder never accumulates several campaigns' takes. The pointer is a tiny JSON file inside the
# private workspace; no pointer (or an invalid one) falls back to the bare inbox, which the
# hygiene checker then flags. See contracts/content-agent/workspace-organization-v1.md.

PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_ACTIVE_PROJECT_POINTER = Path("projects") / ".active-project.json"


def _pointer_path(layout: ContentAgentLayout) -> Path:
    return layout.workspace / _ACTIVE_PROJECT_POINTER


def active_project() -> str | None:
    """The active project slug, or None (no layout, no pointer, or an invalid pointer)."""
    layout = content_agent_layout()
    if layout is None:
        return None
    try:
        data = json.loads(_pointer_path(layout).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    slug = data.get("slug") if isinstance(data, dict) else None
    return slug if isinstance(slug, str) and PROJECT_SLUG_RE.match(slug) else None


def set_active_project(slug: str) -> dict:
    """Point new generations at projects/<slug>: creates the project folder and writes the pointer.
    Returns {ok, slug, project_dir, output_dir} or {ok: False, error}."""
    if not isinstance(slug, str) or not PROJECT_SLUG_RE.match(slug):
        return {"ok": False,
                "error": f"invalid project slug: {slug!r}",
                "hint": "kebab-case, 2-63 chars, [a-z0-9-], starts alphanumeric — e.g. 'riwayat-rida-suit-set'"}
    layout = content_agent_layout()
    if layout is None:
        return {"ok": False, "error": "no active content-agent workspace here."}
    project = layout.workspace / "projects" / slug
    project.mkdir(parents=True, exist_ok=True)
    pointer = _pointer_path(layout)
    # unique temp per writer + atomic replace: concurrent set_active_project calls must never
    # collide on a shared temp name or leave a torn pointer (last writer wins cleanly)
    fd, tmp_name = tempfile.mkstemp(dir=pointer.parent, prefix=f".{pointer.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"schema_version": 1, "slug": slug}) + "\n")
        os.replace(tmp_name, pointer)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return {"ok": True, "slug": slug, "project_dir": str(project), "output_dir": output_dir()}


def clear_active_project() -> dict:
    """Remove the pointer; generations fall back to the bare inbox (media/generated)."""
    layout = content_agent_layout()
    if layout is None:
        return {"ok": False, "error": "no active content-agent workspace here."}
    pointer = _pointer_path(layout)
    try:
        pointer.unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "slug": None, "output_dir": output_dir()}


# ---------------------------------------------------------------- cost ledger
# One JSON line per finished generation, appended by client._persist_media. Hidden file at the
# INBOX ROOT (media/generated/), never inside a project bucket — one ledger per workspace, spanning
# projects. Named by contracts/content-agent/workspace-organization-v1.md; the hygiene checker
# treats it as expected.
COST_LEDGER_NAME = ".cost-ledger.jsonl"


def cost_ledger_path() -> Path | None:
    """The workspace's cost ledger file, or None outside a content-agent workspace (public
    fallbacks have no organization contract, so no ledger)."""
    layout = content_agent_layout()
    if layout is None:
        return None
    return layout.workspace / "media" / "generated" / COST_LEDGER_NAME


def output_dir(explicit: str | None = None) -> str:
    """Where durable generated media lands: explicit arg > $SUPERCMO_OUTPUT_DIR > (active project's
    inbox media/generated/<slug>, else media/generated) > ./supercmo-media outside a workspace."""
    slug = None
    if explicit is None and not os.environ.get(OUTPUT_DIR_ENV):
        slug = active_project()
    private_default = f"media/generated/{slug}" if slug else "media/generated"
    return _destination(
        explicit,
        OUTPUT_DIR_ENV,
        _OUTPUT_DEFAULT,
        private_default,
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
