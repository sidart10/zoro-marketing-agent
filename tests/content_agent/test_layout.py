import json
import os
import tempfile
import unittest
from pathlib import Path

from content_agent.layout import (
    ContentAgentLayout,
    ContentAgentLayoutError,
    ContentAgentMarkerInactive,
    PrivacyBoundaryError,
    UnsupportedWorkspaceSchemaError,
    read_workspace_header,
)


VALID_CONFIG = {
    "schema_version": 1,
    "canonical_root_name": "content-agent",
    "workspace": "workspace",
}

VALID_HEADER = {
    "schema_version": 1,
    "workspace_id": "ws_0123456789abcdef0123456789abcdef",
    "created_at": "2026-08-10T12:30:00Z",
}


class ContentAgentLayoutTests(unittest.TestCase):
    def write_config(self, root, config=VALID_CONFIG):
        (root / "content-agent.config.json").write_text(
            json.dumps(config) + "\n", encoding="utf-8"
        )

    def write_header(self, path, header=VALID_HEADER):
        path.write_text(json.dumps(header) + "\n", encoding="utf-8")

    def test_discovers_configured_workspace_from_child(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "content-agent"
            child = root / "skills" / "example"
            child.mkdir(parents=True)
            self.write_config(root)

            layout = ContentAgentLayout.discover(child)

            self.assertEqual(layout.root, root.resolve())
            self.assertEqual(layout.workspace, (root / "workspace").resolve())

    def test_discovers_from_a_file_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "content-agent"
            source_file = root / "skills" / "example" / "SKILL.md"
            source_file.parent.mkdir(parents=True)
            source_file.touch()
            self.write_config(root)

            self.assertEqual(ContentAgentLayout.discover(source_file).root, root.resolve())

    def test_missing_marker_is_an_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ContentAgentLayoutError, "not found"):
                ContentAgentLayout.discover(Path(temporary_directory))

    def test_marker_is_inactive_when_checkout_name_is_not_canonical(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "supercmo-skills"
            root.mkdir()
            self.write_config(root)

            with self.assertRaisesRegex(
                ContentAgentMarkerInactive, "marker is not active"
            ):
                ContentAgentLayout.discover(root)

    def test_rejects_invalid_or_non_object_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "content-agent"
            root.mkdir()
            config_path = root / "content-agent.config.json"

            for invalid_config in ("{", "[]", "null"):
                config_path.write_text(invalid_config, encoding="utf-8")
                with self.subTest(invalid_config=invalid_config):
                    with self.assertRaises(ContentAgentLayoutError):
                        ContentAgentLayout.discover(root)

    def test_rejects_missing_extra_or_wrong_type_config_fields(self):
        invalid_configs = (
            {"canonical_root_name": "content-agent", "workspace": "workspace"},
            {**VALID_CONFIG, "unexpected": True},
            {**VALID_CONFIG, "schema_version": "1"},
            {**VALID_CONFIG, "canonical_root_name": 1},
            {**VALID_CONFIG, "workspace": 1},
            {**VALID_CONFIG, "schema_version": True},
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "content-agent"
            root.mkdir()
            for config in invalid_configs:
                self.write_config(root, config)
                with self.subTest(config=config):
                    with self.assertRaises(ContentAgentLayoutError):
                        ContentAgentLayout.discover(root)

    def test_rejects_unsupported_schema_and_wrong_canonical_config_values(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "content-agent"
            root.mkdir()
            for config in (
                {**VALID_CONFIG, "schema_version": 2},
                {**VALID_CONFIG, "canonical_root_name": "other-root"},
            ):
                self.write_config(root, config)
                with self.subTest(config=config):
                    with self.assertRaises(ContentAgentLayoutError):
                        ContentAgentLayout.discover(root)

    def test_rejects_absolute_and_traversal_workspace_values(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "content-agent"
            root.mkdir()
            for workspace in ("/private/workspace", "../workspace", "nested/../workspace"):
                self.write_config(root, {**VALID_CONFIG, "workspace": workspace})
                with self.subTest(workspace=workspace):
                    with self.assertRaises(ContentAgentLayoutError):
                        ContentAgentLayout.discover(root)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_workspace_symlink_that_escapes_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "content-agent"
            outside_workspace = temporary_root / "outside-workspace"
            root.mkdir()
            outside_workspace.mkdir()
            (root / "workspace").symlink_to(outside_workspace, target_is_directory=True)
            self.write_config(root)

            with self.assertRaises(ContentAgentLayoutError):
                ContentAgentLayout.discover(root)

    def test_rejects_output_outside_private_workspace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "content-agent"
            workspace = root / "workspace"
            workspace.mkdir(parents=True)
            layout = ContentAgentLayout(
                root=root.resolve(), workspace=workspace.resolve()
            )

            with self.assertRaisesRegex(
                PrivacyBoundaryError, "output escapes private workspace"
            ):
                layout.require_private_path(temporary_root / "public-output", "output")

    def test_rejects_prefix_collision_outside_private_workspace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "content-agent"
            workspace = root / "workspace"
            prefix_collision = root / "workspace-public"
            workspace.mkdir(parents=True)
            prefix_collision.mkdir()
            layout = ContentAgentLayout(root=root.resolve(), workspace=workspace.resolve())

            with self.assertRaisesRegex(PrivacyBoundaryError, "output escapes"):
                layout.require_private_path(prefix_collision, "output")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_output_symlink_that_escapes_private_workspace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "content-agent"
            workspace = root / "workspace"
            outside_output = temporary_root / "public-output"
            workspace.mkdir(parents=True)
            outside_output.mkdir()
            (workspace / "output").symlink_to(outside_output, target_is_directory=True)
            layout = ContentAgentLayout(root=root.resolve(), workspace=workspace.resolve())

            with self.assertRaisesRegex(PrivacyBoundaryError, "output escapes"):
                layout.require_private_path(workspace / "output" / "result.png", "output")


class WorkspaceHeaderTests(unittest.TestCase):
    def write_header(self, path, header=VALID_HEADER):
        path.write_text(json.dumps(header) + "\n", encoding="utf-8")

    def test_reads_a_supported_workspace_header(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            header_path = Path(temporary_directory) / "workspace.yaml"
            self.write_header(header_path, {**VALID_HEADER, "display_name": "Creator work"})

            header = read_workspace_header(header_path)

            self.assertEqual(header.schema_version, 1)
            self.assertEqual(header.workspace_id, VALID_HEADER["workspace_id"])
            self.assertEqual(header.created_at, VALID_HEADER["created_at"])
            self.assertEqual(header.display_name, "Creator work")

    def test_reads_a_supported_workspace_header_without_display_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            header_path = Path(temporary_directory) / "workspace.yaml"
            self.write_header(header_path)

            header = read_workspace_header(header_path)

            self.assertIsNone(header.display_name)

    def test_rejects_null_display_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            header_path = Path(temporary_directory) / "workspace.yaml"
            self.write_header(header_path, {**VALID_HEADER, "display_name": None})

            with self.assertRaisesRegex(
                ContentAgentLayoutError, "display_name must be a string"
            ):
                read_workspace_header(header_path)

    def test_rejects_invalid_non_object_missing_extra_or_wrong_type_header(self):
        invalid_headers = (
            "{",
            "[]",
            "null",
            {"workspace_id": VALID_HEADER["workspace_id"], "created_at": VALID_HEADER["created_at"]},
            {**VALID_HEADER, "unexpected": True},
            {**VALID_HEADER, "schema_version": "1"},
            {**VALID_HEADER, "workspace_id": 1},
            {**VALID_HEADER, "created_at": 1},
            {**VALID_HEADER, "display_name": 1},
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            header_path = Path(temporary_directory) / "workspace.yaml"
            for header in invalid_headers:
                contents = header if isinstance(header, str) else json.dumps(header)
                header_path.write_text(contents, encoding="utf-8")
                with self.subTest(header=header):
                    with self.assertRaises(ContentAgentLayoutError):
                        read_workspace_header(header_path)

    def test_rejects_invalid_workspace_id_and_malformed_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            header_path = Path(temporary_directory) / "workspace.yaml"
            for header in (
                {**VALID_HEADER, "workspace_id": "workspace_0123456789abcdef0123456789abcdef"},
                {**VALID_HEADER, "workspace_id": "ws_0123456789ABCDEF0123456789abcdef"},
                {**VALID_HEADER, "created_at": "2026-13-10T12:30:00Z"},
                {**VALID_HEADER, "created_at": "not-a-timestamp"},
            ):
                self.write_header(header_path, header)
                with self.subTest(header=header):
                    with self.assertRaises(ContentAgentLayoutError):
                        read_workspace_header(header_path)

    def test_rejects_unsupported_workspace_schema_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            header_path = Path(temporary_directory) / "workspace.yaml"
            header = {**VALID_HEADER, "schema_version": 2}
            self.write_header(header_path, header)
            before = header_path.read_bytes()

            with self.assertRaises(UnsupportedWorkspaceSchemaError):
                read_workspace_header(header_path)

            self.assertEqual(header_path.read_bytes(), before)
