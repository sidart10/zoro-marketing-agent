#!/usr/bin/env python3
"""Command-line entry points for the private Content Agent workspace."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from content_agent.evaluation_migration import copy_evaluation, require_path_component
from content_agent.layout import ContentAgentLayout, ContentAgentLayoutError
from content_agent.workspace import initialize_workspace, validate_inner_staging
from supercmo_skills import paths


def _print_json(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True))


def _initialize(arguments: argparse.Namespace) -> int:
    layout = ContentAgentLayout.discover(Path.cwd())
    receipt = initialize_workspace(layout, arguments.workspace_id, datetime.now(timezone.utc))
    _print_json(
        {
            "workspace": str(receipt.workspace),
            "created": receipt.created,
            "schema_version": receipt.schema_version,
            "directories": list(receipt.directories),
        }
    )
    return 0


def _validate(arguments: argparse.Namespace) -> int:
    del arguments
    layout = ContentAgentLayout.discover(Path.cwd())
    errors = validate_inner_staging(layout.workspace)
    _print_json({"valid": not errors, "errors": errors})
    return 0 if not errors else 1


def _path(arguments: argparse.Namespace) -> int:
    destinations = {
        "output": paths.output_dir,
        "scratch": paths.scratch_dir,
        "cache": paths.cache_dir,
        "projection": paths.projection_dir,
    }
    print(destinations[arguments.kind]())
    return 0


def _migrate_evaluation(arguments: argparse.Namespace) -> int:
    layout = ContentAgentLayout.discover(Path.cwd())
    suite_id = require_path_component(arguments.suite_id, "suite-id")
    iteration = require_path_component(arguments.iteration, "iteration")
    receipt = copy_evaluation(
        Path(arguments.source),
        layout.workspace / "evaluations" / suite_id / iteration,
        layout,
    )
    _print_json(
        {
            "source": str(receipt.source),
            "destination": str(receipt.destination),
            "source_count": receipt.source_count,
            "destination_count": receipt.destination_count,
            "source_bytes": receipt.source_bytes,
            "destination_bytes": receipt.destination_bytes,
            "inventory_sha256": receipt.inventory_sha256,
            "excluded_paths": list(receipt.excluded_paths),
            "source_preserved": receipt.source_preserved,
            "idempotent": receipt.idempotent,
        }
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--workspace-id", required=True)
    initialize.set_defaults(handler=_initialize)
    validate = commands.add_parser("validate")
    validate.set_defaults(handler=_validate)
    path = commands.add_parser("path")
    path.add_argument("--kind", choices=("output", "scratch", "cache", "projection"), required=True)
    path.set_defaults(handler=_path)
    migrate_evaluation = commands.add_parser("migrate-evaluation")
    migrate_evaluation.add_argument("--source", required=True)
    migrate_evaluation.add_argument("--suite-id", required=True)
    migrate_evaluation.add_argument("--iteration", required=True)
    migrate_evaluation.set_defaults(handler=_migrate_evaluation)
    arguments = parser.parse_args()
    try:
        return arguments.handler(arguments)
    except ContentAgentLayoutError as error:
        _print_json({"errors": [str(error)]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
