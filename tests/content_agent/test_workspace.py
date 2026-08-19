import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from content_agent.layout import (
    ContentAgentLayout,
    ContentAgentLayoutError,
    UnsupportedWorkspaceSchemaError,
)
from content_agent.workspace import (
    INNER_IGNORE,
    WORKSPACE_DIRECTORIES,
    initialize_workspace,
    validate_inner_staging,
)


REPOSITORY = Path(__file__).resolve().parents[2]
WORKSPACE_ID = "ws_00000000000000000000000000000001"
FIXED_TIME = datetime(2026, 8, 10, tzinfo=timezone.utc)


class WorkspaceInitializerTests(unittest.TestCase):
    def create_layout(self, temporary_directory):
        root = Path(temporary_directory) / "content-agent"
        root.mkdir()
        (root / "content-agent.config.json").write_text(
            '{"schema_version":1,"canonical_root_name":"content-agent","workspace":"workspace"}\n',
            encoding="utf-8",
        )
        (root / ".gitignore").write_text("/workspace/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        return ContentAgentLayout.discover(root)

    def test_initializer_creates_private_skeleton_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = self.create_layout(temporary_directory)
            workspace_id = WORKSPACE_ID
            fixed_time = FIXED_TIME

            receipt1 = initialize_workspace(layout, workspace_id, fixed_time)
            receipt2 = initialize_workspace(layout, workspace_id, fixed_time)

            self.assertTrue(receipt1.created)
            self.assertFalse(receipt2.created)
            self.assertEqual(
                json.loads((layout.workspace / "workspace.yaml").read_text()),
                {
                    "schema_version": 1,
                    "workspace_id": workspace_id,
                    "created_at": "2026-08-10T00:00:00Z",
                },
            )
            self.assertEqual(
                (layout.workspace / ".gitignore").read_text(), INNER_IGNORE
            )

    def test_initializer_creates_only_declared_directories_and_safe_markers(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = self.create_layout(temporary_directory)

            receipt = initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)

            self.assertEqual(receipt.directories, WORKSPACE_DIRECTORIES)
            self.assertEqual(
                {path.name for path in layout.workspace.iterdir() if path.is_dir()},
                set(WORKSPACE_DIRECTORIES),
            )
            for directory in WORKSPACE_DIRECTORIES:
                with self.subTest(directory=directory):
                    self.assertEqual(
                        (layout.workspace / directory / ".keep").exists(),
                        directory not in {"media", "cache", "secrets"},
                    )

    def test_initializer_refuses_existing_different_workspace_id(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = self.create_layout(temporary_directory)
            initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)
            before = (layout.workspace / "workspace.yaml").read_bytes()

            with self.assertRaisesRegex(ContentAgentLayoutError, "different workspace_id"):
                initialize_workspace(
                    layout,
                    "ws_00000000000000000000000000000002",
                    FIXED_TIME,
                )

            self.assertEqual((layout.workspace / "workspace.yaml").read_bytes(), before)

    def test_initializer_refuses_an_invalid_workspace_id_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = self.create_layout(temporary_directory)

            with self.assertRaisesRegex(ContentAgentLayoutError, "workspace_id must match"):
                initialize_workspace(layout, "invalid", FIXED_TIME)

            self.assertFalse((layout.workspace / "workspace.yaml").exists())

    def test_initializer_refuses_existing_unsupported_schema_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = self.create_layout(temporary_directory)
            layout.workspace.mkdir()
            header_path = layout.workspace / "workspace.yaml"
            header_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "workspace_id": WORKSPACE_ID,
                        "created_at": "2026-08-10T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before = header_path.read_bytes()

            with self.assertRaises(UnsupportedWorkspaceSchemaError):
                initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)

            self.assertEqual(header_path.read_bytes(), before)

    def test_initializer_fails_closed_when_completed_workspace_is_incomplete_or_altered(self):
        for scenario in (
            "missing ignore policy",
            "altered ignore policy",
            "missing directory",
            "altered marker",
            "marker in ignored directory",
        ):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary_directory:
                layout = self.create_layout(temporary_directory)
                initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)
                if scenario == "missing ignore policy":
                    (layout.workspace / ".gitignore").unlink()
                elif scenario == "altered ignore policy":
                    (layout.workspace / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
                elif scenario == "missing directory":
                    (layout.workspace / "channels" / ".keep").unlink()
                    (layout.workspace / "channels").rmdir()
                elif scenario == "altered marker":
                    (layout.workspace / "projects" / ".keep").write_text(
                        "altered", encoding="utf-8"
                    )
                else:
                    (layout.workspace / "media" / ".keep").touch()

                with self.assertRaisesRegex(
                    ContentAgentLayoutError, "incomplete or altered"
                ):
                    initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)

    def test_initializer_uses_private_permissions_and_never_initializes_inner_git(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = self.create_layout(temporary_directory)

            initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)

            self.assertFalse((layout.workspace / ".git").exists())
            self.assertEqual((layout.workspace / "workspace.yaml").stat().st_mode & 0o777, 0o600)
            self.assertEqual((layout.workspace / ".gitignore").stat().st_mode & 0o777, 0o600)

    def test_initializer_removes_temporary_file_when_atomic_header_replace_fails(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = self.create_layout(temporary_directory)
            from content_agent import workspace as workspace_module

            original_replace = workspace_module.os.replace

            def fail_header_replace(source, destination):
                if Path(destination).name == "workspace.yaml":
                    raise OSError("replace failed")
                return original_replace(source, destination)

            with patch("content_agent.workspace.os.replace", side_effect=fail_header_replace):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)

            self.assertFalse((layout.workspace / "workspace.yaml").exists())
            self.assertEqual(list(layout.workspace.glob(".workspace.yaml.*.tmp")), [])

    def test_initializer_can_resume_after_atomic_ignore_policy_write_fails(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = self.create_layout(temporary_directory)
            from content_agent import workspace as workspace_module

            original_replace = workspace_module.os.replace

            def fail_ignore_replace(source, destination):
                if Path(destination).name == ".gitignore":
                    raise OSError("ignore replace failed")
                return original_replace(source, destination)

            with patch("content_agent.workspace.os.replace", side_effect=fail_ignore_replace):
                with self.assertRaisesRegex(OSError, "ignore replace failed"):
                    initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)

            self.assertFalse((layout.workspace / "workspace.yaml").exists())
            self.assertEqual(list(layout.workspace.glob("..gitignore.*.tmp")), [])

            receipt = initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)

            self.assertTrue(receipt.created)
            self.assertEqual((layout.workspace / ".gitignore").read_text(), INNER_IGNORE)

    def test_initializer_refuses_headerless_partial_workspace_with_user_data(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            layout = self.create_layout(temporary_directory)
            draft = layout.workspace / "channels" / "draft.md"
            draft.parent.mkdir(parents=True)
            draft.write_text("do not overwrite", encoding="utf-8")

            with self.assertRaisesRegex(ContentAgentLayoutError, "conflicting data"):
                initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)

            self.assertEqual(draft.read_text(encoding="utf-8"), "do not overwrite")
            self.assertFalse((layout.workspace / "workspace.yaml").exists())

    def test_initializer_refuses_to_create_a_workspace_without_outer_isolation(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "content-agent"
            root.mkdir()
            (root / "content-agent.config.json").write_text(
                '{"schema_version":1,"canonical_root_name":"content-agent","workspace":"workspace"}\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            layout = ContentAgentLayout.discover(root)

            with self.assertRaisesRegex(ContentAgentLayoutError, "not ignored"):
                initialize_workspace(layout, WORKSPACE_ID, FIXED_TIME)

            self.assertFalse(layout.workspace.exists())


class InnerStagingValidationTests(unittest.TestCase):
    def create_inner_repository(self, temporary_directory):
        workspace = Path(temporary_directory) / "content-agent" / "workspace"
        workspace.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        return workspace

    def stage(self, workspace, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"canary")
        subprocess.run(["git", "add", "-f", str(path.relative_to(workspace))], cwd=workspace, check=True)

    def test_validation_allows_safe_staged_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = self.create_inner_repository(temporary_directory)
            self.stage(workspace, workspace / "channels" / "brief.md")

            self.assertEqual(validate_inner_staging(workspace), [])

    def test_validation_rejects_protected_directories_and_canaries_outside_them(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = self.create_inner_repository(temporary_directory)
            for relative_path in (
                "media/clip.txt",
                "cache/entry.txt",
                "secrets/token.txt",
                "channels/.env.production",
                "projects/clip.mp4",
                "projects/node_modules/package.js",
                "library/provider-payload.json",
                "library/__pycache__/module.pyc",
                "evaluations/signed-url.txt",
            ):
                self.stage(workspace, workspace / relative_path)

            self.assertEqual(
                validate_inner_staging(workspace),
                [
                    "inner staging: protected staged path: cache/entry.txt",
                    "inner staging: protected staged path: channels/.env.production",
                    "inner staging: protected staged path: evaluations/signed-url.txt",
                    "inner staging: protected staged path: library/__pycache__/module.pyc",
                    "inner staging: protected staged path: library/provider-payload.json",
                    "inner staging: protected staged path: media/clip.txt",
                    "inner staging: protected staged path: projects/clip.mp4",
                    "inner staging: protected staged path: projects/node_modules/package.js",
                    "inner staging: protected staged path: secrets/token.txt",
                ],
            )


class WorkspaceCliTests(unittest.TestCase):
    def create_root(self, temporary_directory):
        root = Path(temporary_directory) / "content-agent"
        root.mkdir()
        (root / "content-agent.config.json").write_text(
            '{"schema_version":1,"canonical_root_name":"content-agent","workspace":"workspace"}\n',
            encoding="utf-8",
        )
        (root / ".gitignore").write_text("/workspace/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        return root

    def run_cli(self, root, *arguments):
        return subprocess.run(
            ["python3", str(REPOSITORY / "scripts" / "content_agent_cli.py"), *arguments],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_init_prints_json_receipt_without_private_contents(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.create_root(temporary_directory)

            result = self.run_cli(root, "init", "--workspace-id", WORKSPACE_ID)

            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertTrue(receipt["created"])
            self.assertEqual(receipt["schema_version"], 1)
            self.assertEqual(receipt["directories"], list(WORKSPACE_DIRECTORIES))
            self.assertNotIn("workspace_id", result.stdout)

    def test_validate_prints_deterministic_json_and_fails_for_protected_staging(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = self.create_root(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            secret = workspace / "channels" / "secret.txt"
            secret.parent.mkdir()
            secret.write_text("canary", encoding="utf-8")
            subprocess.run(["git", "add", "secret.txt"], cwd=secret.parent, check=True)

            result = self.run_cli(root, "validate")

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "errors": [
                        "inner staging: protected staged path: channels/secret.txt"
                    ],
                    "valid": False,
                },
            )
