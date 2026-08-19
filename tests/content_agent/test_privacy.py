import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

import package_plugins
from content_agent.layout import ContentAgentLayout, PrivacyBoundaryError
from content_agent.privacy import (
    assert_public_source,
    validate_outer_isolation,
    validate_package_entries,
)


def initialize_git_repository(parent: Path, *, ignore_workspace: bool) -> Path:
    repository = parent / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    if ignore_workspace:
        (repository / ".gitignore").write_text("/workspace/\n", encoding="utf-8")
    return repository


def initialize_package_fixture(parent: Path, *, ignore_workspace: bool) -> Path:
    repository = initialize_git_repository(
        parent, ignore_workspace=ignore_workspace
    )
    plugin_directory = repository / ".claude-plugin"
    plugin_directory.mkdir()
    (plugin_directory / "plugin.json").write_text(
        json.dumps({"name": "test-plugin"}) + "\n", encoding="utf-8"
    )
    workspace = repository / "workspace"
    workspace.mkdir()
    (workspace / "private-canary.txt").write_text("private", encoding="utf-8")
    return repository


class PrivateWorkspaceIsolationTests(unittest.TestCase):
    def test_outer_git_cannot_see_ignored_workspace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = initialize_git_repository(
                Path(temporary_directory), ignore_workspace=True
            )
            workspace = repository / "workspace"
            workspace.mkdir()
            (workspace / "private-canary.txt").write_text(
                "private", encoding="utf-8"
            )

            status = subprocess.run(
                ["git", "status", "--short", "--untracked-files=all"],
                cwd=repository,
                text=True,
                capture_output=True,
                check=True,
            ).stdout

            self.assertNotIn("workspace", status)

    def test_packager_refuses_repository_without_workspace_isolation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = initialize_package_fixture(
                Path(temporary_directory), ignore_workspace=False
            )

            with self.assertRaisesRegex(RuntimeError, "workspace isolation"):
                package_plugins.package(repository)

            self.assertFalse((repository / "dist" / "test-plugin-plugin.zip").exists())

    def test_packager_never_contains_workspace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = initialize_package_fixture(
                Path(temporary_directory), ignore_workspace=True
            )

            self.assertTrue(package_plugins.package(repository))
            bundle = repository / "dist" / "test-plugin-plugin.zip"
            with zipfile.ZipFile(bundle) as archive:
                self.assertFalse(
                    any(name.startswith("workspace/") for name in archive.namelist())
                )

    def test_packager_never_contains_native_rename_probe_markers(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = initialize_package_fixture(
                Path(temporary_directory), ignore_workspace=True
            )
            for name in (
                ".content-agent-rename-probe-a.tmp",
                ".content-agent-rename-probe-b.tmp",
            ):
                (repository / name).write_bytes(b"")

            self.assertTrue(package_plugins.package(repository))
            bundle = repository / "dist" / "test-plugin-plugin.zip"
            with zipfile.ZipFile(bundle) as archive:
                self.assertFalse(
                    any("content-agent-rename-probe" in name for name in archive.namelist())
                )

    def test_packager_rejects_nested_symlink_to_private_workspace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = initialize_package_fixture(
                Path(temporary_directory), ignore_workspace=True
            )
            skills = repository / "skills"
            skills.mkdir()
            (skills / "private").symlink_to(
                repository / "workspace", target_is_directory=True
            )

            with self.assertRaisesRegex(RuntimeError, "symlink"):
                package_plugins.package(repository)

            self.assertFalse((repository / "dist" / "test-plugin-plugin.zip").exists())

    def test_packager_preserves_existing_bundle_when_source_preflight_fails(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = initialize_package_fixture(
                Path(temporary_directory), ignore_workspace=True
            )
            skills = repository / "skills"
            skills.mkdir()
            (skills / "private").symlink_to(
                repository / "workspace", target_is_directory=True
            )
            bundle = repository / "dist" / "test-plugin-plugin.zip"
            bundle.parent.mkdir()
            previous_bundle = b"previous valid bundle"
            bundle.write_bytes(previous_bundle)

            with self.assertRaisesRegex(RuntimeError, "symlink"):
                package_plugins.package(repository)

            self.assertEqual(bundle.read_bytes(), previous_bundle)

    def test_outer_isolation_rejects_tracked_workspace_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = initialize_package_fixture(
                Path(temporary_directory), ignore_workspace=True
            )
            subprocess.run(
                ["git", "add", "-f", "workspace/private-canary.txt"],
                cwd=repository,
                check=True,
            )
            layout = ContentAgentLayout(
                root=repository.resolve(),
                workspace=(repository / "workspace").resolve(),
            )

            errors = validate_outer_isolation(layout)

            self.assertTrue(any("tracked private path" in error for error in errors))

    def test_outer_isolation_rejects_workspace_in_generated_tree(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = initialize_git_repository(
                Path(temporary_directory), ignore_workspace=True
            )
            layout = ContentAgentLayout(
                root=repository.resolve(),
                workspace=(repository / "dist" / "workspace").resolve(),
            )

            errors = validate_outer_isolation(layout)

            self.assertTrue(any("generated tree" in error for error in errors))

    def test_public_sources_and_package_entries_reject_private_traversal(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            repository = initialize_package_fixture(
                Path(temporary_directory), ignore_workspace=True
            )
            layout = ContentAgentLayout(
                root=repository.resolve(),
                workspace=(repository / "workspace").resolve(),
            )

            with self.assertRaises(PrivacyBoundaryError):
                assert_public_source(
                    repository / "workspace" / "private-canary.txt",
                    layout,
                    "package source",
                )

            errors = validate_package_entries(
                ("skills", "workspace", "../outside", "/absolute")
            )

            self.assertEqual(len(errors), 3)
