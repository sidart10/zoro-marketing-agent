import errno
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from content_agent.layout import ContentAgentLayout, PrivacyBoundaryError
from content_agent.evaluation_migration import (
    EvaluationMigrationError,
    _lock_name,
    _rename_noreplace,
    _temp_prefix,
    copy_evaluation,
)


class EvaluationMigrationTests(unittest.TestCase):
    """A broken copy, boundary, or receipt must stop the migration safely."""

    def create_fixture(self, temporary_directory: str) -> tuple[Path, Path, ContentAgentLayout]:
        root = (Path(temporary_directory) / "content-agent").resolve()
        source = root / "source"
        source.mkdir(parents=True)
        layout = ContentAgentLayout(
            root=root.resolve(), workspace=(root / "workspace").resolve()
        )
        return root, source, layout

    def destination(self, layout: ContentAgentLayout) -> Path:
        return layout.workspace / "evaluations" / "zoro-audition" / "iteration-1"

    def test_copy_evaluation_verifies_every_payload_and_writes_canonical_inventory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "outputs").mkdir()
            (source / "outputs" / "scenario-01.json").write_bytes(b'{"result":"pass"}\n')
            (source / "labels.json").write_bytes(b'{"scenario-01":"pass"}\n')
            destination = self.destination(layout)
            source_before = {
                path.relative_to(source).as_posix(): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }

            receipt = copy_evaluation(source, destination, layout)

            expected_inventory = (
                b'[{"bytes":23,"path":"labels.json","sha256":"'
                + hashlib.sha256(b'{"scenario-01":"pass"}\n').hexdigest().encode()
                + b'"},{"bytes":18,"path":"outputs/scenario-01.json","sha256":"'
                + hashlib.sha256(b'{"result":"pass"}\n').hexdigest().encode()
                + b'"}]\n'
            )
            self.assertEqual(receipt.source_count, 2)
            self.assertEqual(receipt.destination_count, 2)
            self.assertEqual(receipt.source_bytes, 41)
            self.assertEqual(receipt.destination_bytes, 41)
            self.assertEqual(receipt.mismatches, ())
            self.assertTrue(receipt.source_preserved)
            self.assertFalse(receipt.idempotent)
            self.assertEqual(receipt.inventory_path.read_bytes(), expected_inventory)
            self.assertEqual(
                receipt.inventory_sha256,
                hashlib.sha256(expected_inventory).hexdigest(),
            )
            self.assertEqual(
                json.loads(receipt.receipt_path.read_text(encoding="utf-8"))["source_count"], 2
            )
            self.assertEqual(
                {
                    path.relative_to(source).as_posix(): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file()
                },
                source_before,
            )

            retained = [
                path for path in destination.parent.iterdir() if path != destination
            ]
            self.assertEqual(len(retained), 1)
            self.assertTrue(retained[0].is_file())
            self.assertTrue(retained[0].name.startswith(".retained-lock-"))
            self.assertTrue(retained[0].name.endswith(".recovery.tmp"))
            self.assertFalse((layout.workspace / "cache").exists())

    def test_existing_destination_with_different_payload_blocks_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            destination.mkdir(parents=True)
            destination_payload = destination / "labels.json"
            destination_payload.write_bytes(b"different")

            with self.assertRaisesRegex(EvaluationMigrationError, "does not match"):
                copy_evaluation(source, destination, layout)

            self.assertEqual(destination_payload.read_bytes(), b"different")
            self.assertFalse((destination / "migration-receipt.json").exists())
            self.assertEqual((source / "labels.json").read_bytes(), b"source")

    def test_identical_second_run_is_idempotent_and_does_not_rewrite_receipt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)

            first = copy_evaluation(source, destination, layout)
            receipt_before = first.receipt_path.read_bytes()
            second = copy_evaluation(source, destination, layout)

            self.assertTrue(second.idempotent)
            self.assertEqual(second.inventory_sha256, first.inventory_sha256)
            self.assertEqual(second.receipt_path.read_bytes(), receipt_before)

    def test_long_legal_destination_name_uses_bounded_retained_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = (
                layout.workspace / "evaluations" / "zoro-audition" / ("i" * 240)
            )

            first = copy_evaluation(source, destination, layout)
            second = copy_evaluation(source, destination, layout)

            self.assertTrue(first.destination.is_dir())
            self.assertTrue(second.idempotent)
            retained = [
                path for path in destination.parent.iterdir() if path != destination
            ]
            self.assertEqual(len(retained), 1)
            self.assertLessEqual(len(os.fsencode(retained[0].name)), 255)
            self.assertTrue(retained[0].name.endswith(".recovery.tmp"))

    def test_idempotent_run_rejects_every_corrupted_receipt_field(self):
        mutations = {
            "schema_version": 999,
            "source": "/synthetic/wrong-source",
            "destination": "/synthetic/wrong-destination",
            "source_count": 999,
            "destination_count": 999,
            "source_bytes": 999,
            "destination_bytes": 999,
            "inventory_sha256": "0" * 64,
            "excluded_paths": ["unexpected.pyc"],
            "source_preserved": False,
            "timestamp": {"not": "a timestamp"},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            first = copy_evaluation(source, destination, layout)
            original_bytes = first.receipt_path.read_bytes()
            original = json.loads(original_bytes)

            for field, replacement in mutations.items():
                with self.subTest(field=field):
                    corrupted = {**original, field: replacement}
                    first.receipt_path.write_text(
                        json.dumps(corrupted, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(EvaluationMigrationError, "does not match"):
                        copy_evaluation(source, destination, layout)
                    first.receipt_path.write_bytes(original_bytes)

            incomplete = {"inventory_sha256": original["inventory_sha256"]}
            first.receipt_path.write_text(
                json.dumps(incomplete, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvaluationMigrationError, "does not match"):
                copy_evaluation(source, destination, layout)

            first.receipt_path.write_text(
                json.dumps(original, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvaluationMigrationError, "does not match"):
                copy_evaluation(source, destination, layout)

    def test_idempotent_run_rejects_unexpected_excluded_destination_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            copy_evaluation(source, destination, layout)
            (destination / "unexpected.pyc").write_bytes(b"cache drift")

            with self.assertRaisesRegex(EvaluationMigrationError, "does not match"):
                copy_evaluation(source, destination, layout)

    def test_destination_outside_private_workspace_is_rejected_before_copying(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            escaped_destination = root / "public" / "iteration-1"

            with self.assertRaises(PrivacyBoundaryError):
                copy_evaluation(source, escaped_destination, layout)

            self.assertFalse(escaped_destination.exists())
            self.assertTrue((source / "labels.json").exists())

    def test_unsupported_platform_fails_before_creating_migration_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)

            with patch("content_agent.evaluation_migration.sys.platform", "win32"):
                with self.assertRaisesRegex(EvaluationMigrationError, "Darwin and Linux"):
                    copy_evaluation(source, destination, layout)

            self.assertFalse(destination.exists())
            self.assertFalse(layout.workspace.exists())

    def test_missing_native_rename_primitive_fails_before_creating_migration_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)

            with patch(
                "content_agent.evaluation_migration.ctypes.CDLL",
                return_value=SimpleNamespace(),
            ):
                with self.assertRaisesRegex(EvaluationMigrationError, "unavailable"):
                    copy_evaluation(source, destination, layout)

            self.assertFalse(destination.exists())
            self.assertFalse(layout.workspace.exists())

    def test_native_rename_probe_failure_precedes_workspace_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)

            with patch(
                "content_agent.evaluation_migration._probe_native_rename",
                side_effect=EvaluationMigrationError("primitive unavailable"),
            ):
                with self.assertRaisesRegex(EvaluationMigrationError, "unavailable"):
                    copy_evaluation(source, destination, layout)

            self.assertFalse(destination.exists())
            self.assertFalse(layout.workspace.exists())

    def test_native_rename_probe_never_uses_an_unrelated_existing_entry(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_NativeRename", "_probe_native_rename"],
        )
        primitive = module._NativeRename(
            library=object(), function=object(), flags=1
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            (parent / "existing-entry").write_bytes(b"unchanged")
            descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                calls: list[tuple[str, str]] = []

                def emulate_no_replace(
                    primitive: object,
                    source_descriptor: int,
                    source_name: str,
                    destination_descriptor: int,
                    destination_name: str,
                ) -> tuple[int, int]:
                    del primitive
                    calls.append((source_name, destination_name))
                    if source_name == "":
                        return -1, errno.ENOENT
                    try:
                        os.stat(
                            destination_name,
                            dir_fd=destination_descriptor,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        return -1, errno.EEXIST
                    os.rename(
                        source_name,
                        destination_name,
                        src_dir_fd=source_descriptor,
                        dst_dir_fd=destination_descriptor,
                    )
                    return 0, 0

                with patch(
                    "content_agent.evaluation_migration._invoke_native_rename",
                    side_effect=emulate_no_replace,
                ):
                    module._probe_native_rename(primitive, descriptor)
            finally:
                os.close(descriptor)
            existing_payload = (parent / "existing-entry").read_bytes()

        self.assertEqual(existing_payload, b"unchanged")
        self.assertEqual(calls[0], ("", ""))
        self.assertEqual(
            calls[1],
            (
                ".content-agent-rename-probe-a.tmp",
                ".content-agent-rename-probe-b.tmp",
            ),
        )

    def test_same_path_short_circuit_cannot_mask_unsupported_real_rename(self):
        same_path_results = ((-1, errno.EEXIST), (0, 0))
        for same_path_result in same_path_results:
            with self.subTest(same_path_result=same_path_result), tempfile.TemporaryDirectory(
                ) as temporary_directory:
                root, source, layout = self.create_fixture(temporary_directory)
                (source / "labels.json").write_bytes(b"source")
                destination = self.destination(layout)

                def reject_distinct_names(
                    primitive: object,
                    source_descriptor: int,
                    source_name: str,
                    destination_descriptor: int,
                    destination_name: str,
                ) -> tuple[int, int]:
                    del primitive, source_descriptor, destination_descriptor
                    if source_name == "":
                        return -1, errno.ENOENT
                    if source_name == destination_name:
                        return same_path_result
                    return -1, errno.ENOTSUP

                with patch(
                    "content_agent.evaluation_migration._invoke_native_rename",
                    side_effect=reject_distinct_names,
                ):
                    with self.assertRaisesRegex(EvaluationMigrationError, "unavailable"):
                        copy_evaluation(source, destination, layout)

                self.assertFalse(destination.exists())
                self.assertFalse(layout.workspace.exists())
                markers = sorted(
                    root.glob(".content-agent-rename-probe-*.tmp")
                )
                self.assertEqual(
                    [marker.name for marker in markers],
                    [".content-agent-rename-probe-a.tmp"],
                )
                self.assertEqual(markers[0].read_bytes(), b"")

    def test_repeated_preflight_toggles_exactly_one_bounded_public_marker(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)

            copy_evaluation(source, destination, layout)
            first_markers = sorted(
                root.rglob(".content-agent-rename-probe-*.tmp")
            )
            self.assertEqual(
                [marker.name for marker in first_markers],
                [".content-agent-rename-probe-b.tmp"],
            )
            self.assertEqual(first_markers[0].read_bytes(), b"")

            copy_evaluation(source, destination, layout)
            second_markers = sorted(
                root.rglob(".content-agent-rename-probe-*.tmp")
            )
            self.assertEqual(
                [marker.name for marker in second_markers],
                [".content-agent-rename-probe-a.tmp"],
            )
            self.assertEqual(second_markers[0].read_bytes(), b"")

    def test_ambiguous_or_invalid_probe_marker_fails_closed_without_workspace(self):
        fixtures = (
            (
                "both",
                {
                    ".content-agent-rename-probe-a.tmp": b"",
                    ".content-agent-rename-probe-b.tmp": b"",
                },
            ),
            (
                "invalid",
                {".content-agent-rename-probe-a.tmp": b"not-empty"},
            ),
        )
        for label, marker_payloads in fixtures:
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                ) as temporary_directory:
                root, source, layout = self.create_fixture(temporary_directory)
                (source / "labels.json").write_bytes(b"source")
                destination = self.destination(layout)
                for name, payload in marker_payloads.items():
                    marker = root / name
                    marker.write_bytes(payload)
                    marker.chmod(0o600)

                with self.assertRaises(EvaluationMigrationError):
                    copy_evaluation(source, destination, layout)

                self.assertFalse(layout.workspace.exists())
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in root.glob(".content-agent-rename-probe-*.tmp")
                    },
                    marker_payloads,
                )

    @unittest.skipUnless(
        hasattr(os, "mkfifo") and getattr(os, "O_NONBLOCK", 0),
        "FIFO and nonblocking opens are unavailable",
    )
    def test_probe_marker_opens_cannot_block_on_fifo_replacement(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_probe_marker_matches"],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            marker_name = ".content-agent-rename-probe-a.tmp"
            marker = root / marker_name
            marker.write_bytes(b"")
            marker.chmod(0o600)
            parent_descriptor = os.open(
                root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            original_open = os.open
            observed_flags: list[int] = []

            def replace_with_fifo(
                name: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                if name == marker_name:
                    marker.unlink()
                    os.mkfifo(marker, 0o600)
                    observed_flags.append(flags)
                    if not flags & os.O_NONBLOCK:
                        raise AssertionError("probe marker open can block")
                return original_open(name, flags, mode, dir_fd=dir_fd)

            try:
                with patch(
                    "content_agent.evaluation_migration.os.open",
                    side_effect=replace_with_fifo,
                ):
                    matches, _ = module._probe_marker_matches(
                        parent_descriptor, marker_name
                    )

                self.assertFalse(matches)
                self.assertEqual(len(observed_flags), 1)
                self.assertTrue(observed_flags[0] & os.O_NONBLOCK)
            finally:
                os.close(parent_descriptor)

    @unittest.skipUnless(getattr(os, "O_NONBLOCK", 0), "nonblocking opens are unavailable")
    def test_every_probe_marker_open_is_nonblocking(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_require_secure_platform"],
        )
        primitive = module._require_secure_platform()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            marker = root / ".content-agent-rename-probe-a.tmp"
            marker.write_bytes(b"")
            marker.chmod(0o600)
            original_open = os.open
            observed_flags: list[int] = []

            def observe_marker_open(
                name: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                if name in {
                    ".content-agent-rename-probe-a.tmp",
                    ".content-agent-rename-probe-b.tmp",
                }:
                    observed_flags.append(flags)
                return original_open(name, flags, mode, dir_fd=dir_fd)

            with patch(
                "content_agent.evaluation_migration._require_secure_platform",
                return_value=primitive,
            ), patch(
                "content_agent.evaluation_migration.os.open",
                side_effect=observe_marker_open,
            ):
                copy_evaluation(source, self.destination(layout), layout)

            self.assertGreaterEqual(len(observed_flags), 3)
            self.assertTrue(
                all(flags & getattr(os, "O_NONBLOCK", 0) for flags in observed_flags)
            )

    def test_probe_replacement_is_restored_only_with_no_replace(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_invoke_native_rename"],
        )
        first_name = ".content-agent-rename-probe-a.tmp"
        second_name = ".content-agent-rename-probe-b.tmp"
        for add_restore_collision in (False, True):
            with self.subTest(
                add_restore_collision=add_restore_collision
            ), tempfile.TemporaryDirectory() as temporary_directory:
                root, source, layout = self.create_fixture(temporary_directory)
                (source / "labels.json").write_bytes(b"source")
                destination = self.destination(layout)
                original_invoke = module._invoke_native_rename
                injected = False
                replacement_identity: tuple[int, int] | None = None
                collision_identity: tuple[int, int] | None = None

                def replace_at_distinct_rename(
                    primitive: object,
                    source_descriptor: int,
                    source_name: str,
                    destination_descriptor: int,
                    destination_name: str,
                ) -> tuple[int, int]:
                    nonlocal injected, replacement_identity, collision_identity
                    if source_name == "":
                        return -1, errno.ENOENT
                    if (
                        source_name == first_name
                        and destination_name == second_name
                        and not injected
                    ):
                        os.rename(
                            first_name,
                            "probe-owned-original",
                            src_dir_fd=source_descriptor,
                            dst_dir_fd=source_descriptor,
                        )
                        replacement = root / first_name
                        replacement.write_bytes(b"")
                        replacement.chmod(0o600)
                        status = replacement.stat()
                        replacement_identity = (status.st_dev, status.st_ino)
                        injected = True
                        result = original_invoke(
                            primitive,
                            source_descriptor,
                            source_name,
                            destination_descriptor,
                            destination_name,
                        )
                        if add_restore_collision:
                            collision = root / first_name
                            collision.write_bytes(b"")
                            collision.chmod(0o600)
                            collision_status = collision.stat()
                            collision_identity = (
                                collision_status.st_dev,
                                collision_status.st_ino,
                            )
                        return result
                    return original_invoke(
                        primitive,
                        source_descriptor,
                        source_name,
                        destination_descriptor,
                        destination_name,
                    )

                with patch(
                    "content_agent.evaluation_migration._invoke_native_rename",
                    side_effect=replace_at_distinct_rename,
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError, "ownership changed"
                    ):
                        copy_evaluation(source, destination, layout)

                self.assertFalse(layout.workspace.exists())
                self.assertIsNotNone(replacement_identity)
                if add_restore_collision:
                    self.assertEqual(
                        (root / first_name).stat().st_ino,
                        collision_identity[1],
                    )
                    self.assertEqual(
                        (root / second_name).stat().st_ino,
                        replacement_identity[1],
                    )
                else:
                    self.assertEqual(
                        (root / first_name).stat().st_ino,
                        replacement_identity[1],
                    )
                    self.assertFalse((root / second_name).exists())

    def test_probe_restore_revalidates_identity_after_directory_sync(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_NativeRename", "_restore_probe_marker"],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            moved_name = ".content-agent-rename-probe-b.tmp"
            original_name = ".content-agent-rename-probe-a.tmp"
            moved = root / moved_name
            moved.write_bytes(b"")
            moved.chmod(0o600)
            moved_identity = (moved.stat().st_dev, moved.stat().st_ino)
            parent_descriptor = os.open(
                root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            primitive = module._NativeRename(
                library=object(), function=object(), flags=1
            )
            original_fsync = os.fsync

            def rename_probe(
                primitive_value: object,
                source_descriptor: int,
                source_name: str,
                destination_descriptor: int,
                destination_name: str,
            ) -> tuple[int, int]:
                del primitive_value
                os.rename(
                    source_name,
                    destination_name,
                    src_dir_fd=source_descriptor,
                    dst_dir_fd=destination_descriptor,
                )
                return 0, 0

            def replace_during_sync(file_descriptor: int) -> None:
                restored = root / original_name
                restored.rename(root / "restored-original")
                replacement = root / original_name
                replacement.write_bytes(b"")
                replacement.chmod(0o600)
                original_fsync(file_descriptor)

            try:
                with patch(
                    "content_agent.evaluation_migration._invoke_native_rename",
                    side_effect=rename_probe,
                ), patch(
                    "content_agent.evaluation_migration.os.fsync",
                    side_effect=replace_during_sync,
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError,
                        "changed after.*sync|location.*uncertain",
                    ):
                        module._restore_probe_marker(
                            primitive,
                            parent_descriptor,
                            moved_name,
                            original_name,
                        )

                self.assertEqual(
                    (root / "restored-original").stat().st_ino,
                    moved_identity[1],
                )
                self.assertNotEqual(
                    (root / original_name).stat().st_ino,
                    moved_identity[1],
                )
            finally:
                os.close(parent_descriptor)

    def test_probe_restore_error_branches_never_claim_an_unverified_location(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_NativeRename", "_restore_probe_marker"],
        )
        moved_name = ".content-agent-rename-probe-b.tmp"
        original_name = ".content-agent-rename-probe-a.tmp"
        primitive = module._NativeRename(
            library=object(), function=object(), flags=1
        )

        for phase in ("rename_error", "sync_error"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(
                ) as temporary_directory:
                root = Path(temporary_directory)
                moved = root / moved_name
                moved.write_bytes(b"")
                moved.chmod(0o600)
                parent_descriptor = os.open(
                    root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )

                def rename_probe(
                    primitive_value: object,
                    source_descriptor: int,
                    source_name: str,
                    destination_descriptor: int,
                    destination_name: str,
                ) -> tuple[int, int]:
                    del primitive_value
                    if phase == "rename_error":
                        os.rename(
                            source_name,
                            "moved-elsewhere",
                            src_dir_fd=source_descriptor,
                            dst_dir_fd=source_descriptor,
                        )
                        return -1, errno.ENOENT
                    os.rename(
                        source_name,
                        destination_name,
                        src_dir_fd=source_descriptor,
                        dst_dir_fd=destination_descriptor,
                    )
                    return 0, 0

                def fail_restore_sync(file_descriptor: int) -> None:
                    restored = root / original_name
                    restored.rename(root / "moved-elsewhere")
                    replacement = root / original_name
                    replacement.write_bytes(b"")
                    replacement.chmod(0o600)
                    raise OSError("restore sync failed")

                try:
                    sync_patch = (
                        patch(
                            "content_agent.evaluation_migration.os.fsync",
                            side_effect=fail_restore_sync,
                        )
                        if phase == "sync_error"
                        else patch(
                            "content_agent.evaluation_migration.os.fsync",
                            wraps=os.fsync,
                        )
                    )
                    with patch(
                        "content_agent.evaluation_migration._invoke_native_rename",
                        side_effect=rename_probe,
                    ), sync_patch, self.assertRaises(EvaluationMigrationError) as caught:
                        module._restore_probe_marker(
                            primitive,
                            parent_descriptor,
                            moved_name,
                            original_name,
                        )

                    self.assertIn(
                        "current location is uncertain", str(caught.exception)
                    )
                    self.assertNotIn("preserved at", str(caught.exception))
                    self.assertNotIn("restored to", str(caught.exception))
                finally:
                    os.close(parent_descriptor)

    def test_probe_initialization_sync_failure_closes_owned_descriptor_and_retains_marker(self):
        for failing_call in (1, 2):
            with self.subTest(failing_call=failing_call), tempfile.TemporaryDirectory(
                ) as temporary_directory:
                root, source, layout = self.create_fixture(temporary_directory)
                (source / "labels.json").write_bytes(b"source")
                destination = self.destination(layout)
                original_fsync = os.fsync
                fsync_descriptors: list[int] = []

                def fail_selected_sync(file_descriptor: int) -> None:
                    fsync_descriptors.append(file_descriptor)
                    if len(fsync_descriptors) == failing_call:
                        raise OSError("probe sync failed")
                    original_fsync(file_descriptor)

                with patch(
                    "content_agent.evaluation_migration.os.fsync",
                    side_effect=fail_selected_sync,
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError,
                        r"probe marker retained at \.content-agent-rename-probe-a\.tmp",
                    ):
                        copy_evaluation(source, destination, layout)

                with self.assertRaises(OSError):
                    os.fstat(fsync_descriptors[0])
                marker = root / ".content-agent-rename-probe-a.tmp"
                self.assertEqual(marker.read_bytes(), b"")
                self.assertFalse(layout.workspace.exists())

    def test_probe_marker_names_are_ignored_by_the_public_repository(self):
        repository = Path(__file__).resolve().parents[2]
        for name in (
            ".content-agent-rename-probe-a.tmp",
            ".content-agent-rename-probe-b.tmp",
        ):
            with self.subTest(name=name):
                result = subprocess.run(
                    ["git", "check-ignore", "--quiet", "--no-index", name],
                    cwd=repository,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
                tracked = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", "--", name],
                    cwd=repository,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(tracked.returncode, 0)

    def test_probe_rename_sync_failure_reports_the_moved_marker_and_no_workspace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            original_fsync = os.fsync
            fsync_descriptors: list[int] = []

            def fail_rename_sync(file_descriptor: int) -> None:
                fsync_descriptors.append(file_descriptor)
                if len(fsync_descriptors) == 3:
                    raise OSError("rename sync failed")
                original_fsync(file_descriptor)

            with patch(
                "content_agent.evaluation_migration.os.fsync",
                side_effect=fail_rename_sync,
            ):
                with self.assertRaisesRegex(
                    EvaluationMigrationError,
                    r"moved to \.content-agent-rename-probe-b\.tmp.*durability is uncertain",
                ):
                    copy_evaluation(source, destination, layout)

            with self.assertRaises(OSError):
                os.fstat(fsync_descriptors[0])
            self.assertFalse(
                (root / ".content-agent-rename-probe-a.tmp").exists()
            )
            self.assertEqual(
                (root / ".content-agent-rename-probe-b.tmp").read_bytes(), b""
            )
            self.assertFalse(layout.workspace.exists())

    def test_probe_revalidates_marker_identity_after_directory_sync(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            original_fsync = os.fsync
            fsync_calls = 0
            replacement_identity: tuple[int, int] | None = None

            def replace_during_rename_sync(file_descriptor: int) -> None:
                nonlocal fsync_calls, replacement_identity
                fsync_calls += 1
                if fsync_calls == 3:
                    (root / ".content-agent-rename-probe-b.tmp").rename(
                        root / "probe-owned-before-sync"
                    )
                    replacement = root / ".content-agent-rename-probe-b.tmp"
                    replacement.write_bytes(b"")
                    replacement.chmod(0o600)
                    status = replacement.stat()
                    replacement_identity = (status.st_dev, status.st_ino)
                original_fsync(file_descriptor)

            with patch(
                "content_agent.evaluation_migration.os.fsync",
                side_effect=replace_during_rename_sync,
            ):
                with self.assertRaisesRegex(
                    EvaluationMigrationError, "ownership changed"
                ):
                    copy_evaluation(source, destination, layout)

            self.assertFalse(layout.workspace.exists())
            self.assertIsNotNone(replacement_identity)
            self.assertEqual(
                (root / ".content-agent-rename-probe-a.tmp").stat().st_ino,
                replacement_identity[1],
            )
            self.assertFalse(
                (root / ".content-agent-rename-probe-b.tmp").exists()
            )

    def test_probe_post_rename_status_error_fails_closed_and_restores_durably(self):
        module = __import__(
            "content_agent.evaluation_migration", fromlist=["_entry_status"]
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            original_entry_status = module._entry_status
            second_name_calls = 0

            def fail_post_rename_status(
                parent_descriptor: int, name: str
            ) -> os.stat_result | None:
                nonlocal second_name_calls
                if name == ".content-agent-rename-probe-b.tmp":
                    second_name_calls += 1
                    if second_name_calls == 2:
                        raise OSError("probe status failed")
                return original_entry_status(parent_descriptor, name)

            with patch(
                "content_agent.evaluation_migration._entry_status",
                side_effect=fail_post_rename_status,
            ):
                with self.assertRaisesRegex(
                    EvaluationMigrationError, "ownership changed"
                ):
                    copy_evaluation(source, destination, layout)

            self.assertFalse(layout.workspace.exists())
            self.assertEqual(
                (root / ".content-agent-rename-probe-a.tmp").read_bytes(), b""
            )
            self.assertFalse(
                (root / ".content-agent-rename-probe-b.tmp").exists()
            )

    def test_probe_anchor_traversal_closes_child_descriptor_on_fstat_or_dup_failure(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_open_child_directory"],
        )
        for phase in ("fstat", "dup"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(
                ) as temporary_directory:
                _, source, layout = self.create_fixture(temporary_directory)
                (source / "labels.json").write_bytes(b"source")
                layout.workspace.mkdir()
                destination = self.destination(layout)
                original_open_child = module._open_child_directory
                original_fstat = os.fstat
                original_dup = os.dup
                following_descriptor: int | None = None

                def observe_child(
                    parent_descriptor: int,
                    name: str,
                    expected: os.stat_result,
                ) -> int:
                    nonlocal following_descriptor
                    descriptor = original_open_child(
                        parent_descriptor, name, expected
                    )
                    following_descriptor = descriptor
                    return descriptor

                def fail_or_cross_device(file_descriptor: int) -> os.stat_result:
                    status = original_fstat(file_descriptor)
                    if file_descriptor != following_descriptor:
                        return status
                    if phase == "fstat":
                        raise OSError("anchor fstat failed")
                    values = list(status)
                    values[2] = status.st_dev + 1
                    return os.stat_result(values)

                def fail_anchor_dup(file_descriptor: int) -> int:
                    if phase == "dup" and file_descriptor == following_descriptor:
                        raise OSError("anchor dup failed")
                    return original_dup(file_descriptor)

                try:
                    with patch(
                        "content_agent.evaluation_migration._open_child_directory",
                        side_effect=observe_child,
                    ), patch(
                        "content_agent.evaluation_migration.os.fstat",
                        side_effect=fail_or_cross_device,
                    ), patch(
                        "content_agent.evaluation_migration.os.dup",
                        side_effect=fail_anchor_dup,
                    ):
                        with self.assertRaisesRegex(OSError, f"anchor {phase} failed"):
                            copy_evaluation(source, destination, layout)

                    self.assertIsNotNone(following_descriptor)
                    with self.assertRaises(OSError):
                        os.fstat(following_descriptor)
                finally:
                    if following_descriptor is not None:
                        try:
                            os.close(following_descriptor)
                        except OSError:
                            pass

    def test_relative_creation_rejects_device_transition_before_deeper_state(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_open_relative_directory"],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "first").mkdir()
            root_descriptor = os.open(
                root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            expected_device = os.fstat(root_descriptor).st_dev
            original_open = os.open
            original_fstat = os.fstat
            following_descriptor: int | None = None

            def observe_open(
                name: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal following_descriptor
                descriptor = original_open(name, flags, mode, dir_fd=dir_fd)
                if name == "first":
                    following_descriptor = descriptor
                return descriptor

            def cross_device(file_descriptor: int) -> os.stat_result:
                status = original_fstat(file_descriptor)
                if file_descriptor == following_descriptor:
                    values = list(status)
                    values[2] = status.st_dev + 1
                    return os.stat_result(values)
                return status

            try:
                with patch(
                    "content_agent.evaluation_migration.os.open",
                    side_effect=observe_open,
                ), patch(
                    "content_agent.evaluation_migration.os.fstat",
                    side_effect=cross_device,
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError,
                        "filesystem changed|unprobed filesystem",
                    ):
                        module._open_relative_directory(
                            root_descriptor,
                            ("first", "second"),
                            create=True,
                            expected_device=expected_device,
                        )

                self.assertFalse((root / "first" / "second").exists())
                self.assertIsNotNone(following_descriptor)
                with self.assertRaises(OSError):
                    os.fstat(following_descriptor)
            finally:
                os.close(root_descriptor)
                if following_descriptor is not None:
                    try:
                        os.close(following_descriptor)
                    except OSError:
                        pass

    def test_copy_carries_probed_device_into_every_destination_creation(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_open_relative_directory"],
        )
        original_open_relative = module._open_relative_directory
        observed_devices: list[int | None] = []

        def observe_creation(
            root_descriptor: int,
            components: tuple[str, ...],
            *,
            create: bool = False,
            expected_device: int | None = None,
        ) -> int:
            if create:
                observed_devices.append(expected_device)
            return original_open_relative(
                root_descriptor,
                components,
                create=create,
                expected_device=expected_device,
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "nested").mkdir()
            (source / "nested" / "labels.json").write_bytes(b"source")

            with patch(
                "content_agent.evaluation_migration._open_relative_directory",
                side_effect=observe_creation,
            ):
                copy_evaluation(source, self.destination(layout), layout)

        self.assertGreaterEqual(len(observed_devices), 2)
        self.assertTrue(all(device is not None for device in observed_devices))

    def test_open_child_directory_closes_descriptor_when_internal_fstat_fails(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_open_child_directory"],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            (parent / "child").mkdir()
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            expected = os.stat(
                "child", dir_fd=parent_descriptor, follow_symlinks=False
            )
            original_open = os.open
            opened_descriptors: list[int] = []

            def observe_open(*arguments: object, **keywords: object) -> int:
                descriptor = original_open(*arguments, **keywords)
                opened_descriptors.append(descriptor)
                return descriptor

            try:
                with patch(
                    "content_agent.evaluation_migration.os.open",
                    side_effect=observe_open,
                ), patch(
                    "content_agent.evaluation_migration.os.fstat",
                    side_effect=OSError("child fstat failed"),
                ):
                    with self.assertRaisesRegex(OSError, "child fstat failed"):
                        module._open_child_directory(
                            parent_descriptor, "child", expected
                        )

                self.assertEqual(len(opened_descriptors), 1)
                with self.assertRaises(OSError):
                    os.fstat(opened_descriptors[0])
            finally:
                os.close(parent_descriptor)
                for descriptor in opened_descriptors:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def test_preflighted_native_primitive_is_reused_for_every_rename(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_probe_native_rename", "_rename_noreplace"],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            observed: list[object] = []
            original_probe = module._probe_native_rename
            original_rename = module._rename_noreplace

            def observe_probe(primitive: object, descriptor: int) -> None:
                observed.append(primitive)
                original_probe(primitive, descriptor)

            def observe_rename(*arguments: object, **keywords: object) -> None:
                observed.append(keywords["primitive"])
                original_rename(*arguments, **keywords)

            with patch(
                "content_agent.evaluation_migration._probe_native_rename",
                side_effect=observe_probe,
            ), patch(
                "content_agent.evaluation_migration._rename_noreplace",
                side_effect=observe_rename,
            ):
                copy_evaluation(source, self.destination(layout), layout)

            self.assertGreaterEqual(len(observed), 3)
            self.assertTrue(all(value is observed[0] for value in observed))

    def test_insufficient_free_space_blocks_before_destination_is_created(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            payload = source / "labels.json"
            payload.write_bytes(b"source")
            destination = self.destination(layout)

            with patch(
                "content_agent.evaluation_migration.shutil.disk_usage",
                return_value=SimpleNamespace(free=(len(b"source") * 2) - 1),
            ):
                with self.assertRaisesRegex(EvaluationMigrationError, "insufficient free space"):
                    copy_evaluation(source, destination, layout)

            self.assertFalse(destination.exists())
            self.assertEqual(payload.read_bytes(), b"source")

    def test_headroom_includes_bounded_metadata_overhead(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            payload = source / "labels.json"
            payload.write_bytes(b"source")
            destination = self.destination(layout)

            with patch(
                "content_agent.evaluation_migration.shutil.disk_usage",
                return_value=SimpleNamespace(free=(len(b"source") * 2) + 4095),
            ):
                with self.assertRaisesRegex(EvaluationMigrationError, "insufficient free space"):
                    copy_evaluation(source, destination, layout)

            self.assertFalse(destination.exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_symlink_in_source_is_rejected_without_following_it(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            outside = root / "outside.json"
            outside.write_bytes(b"private elsewhere")
            (source / "labels.json").symlink_to(outside)

            with self.assertRaisesRegex(EvaluationMigrationError, "symlink"):
                copy_evaluation(source, self.destination(layout), layout)

            self.assertFalse(self.destination(layout).exists())
            self.assertEqual(outside.read_bytes(), b"private elsewhere")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_symlink_in_source_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _, layout = self.create_fixture(temporary_directory)
            real_parent = root / "real-parent"
            source = real_parent / "source"
            source.mkdir(parents=True)
            (source / "labels.json").write_bytes(b"source")
            linked_parent = root / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaisesRegex(EvaluationMigrationError, "symlink"):
                copy_evaluation(linked_parent / "source", self.destination(layout), layout)

            self.assertFalse(self.destination(layout).exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_destination_parent_swap_cannot_escape_workspace(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            evaluations = layout.workspace / "evaluations"
            evaluations.mkdir(parents=True)
            outside = root / "outside"
            outside.mkdir()
            original_headroom = __import__(
                "content_agent.evaluation_migration", fromlist=["_require_copy_headroom"]
            )._require_copy_headroom

            def swap_parent(destination: Path, source_bytes: int, *extra: object) -> None:
                original_headroom(destination, source_bytes, *extra)
                evaluations.rename(layout.workspace / "evaluations-original")
                evaluations.symlink_to(outside, target_is_directory=True)

            with patch(
                "content_agent.evaluation_migration._require_copy_headroom",
                side_effect=swap_parent,
            ):
                with self.assertRaises(EvaluationMigrationError):
                    copy_evaluation(source, self.destination(layout), layout)

            self.assertEqual(list(outside.rglob("*")), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_source_ancestor_swap_during_copy_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, _, layout = self.create_fixture(temporary_directory)
            source_parent = root / "source-parent"
            source = source_parent / "source"
            source.mkdir(parents=True)
            (source / "labels.json").write_bytes(b"source")
            outside_parent = root / "outside-parent"
            (outside_parent / "source").mkdir(parents=True)
            (outside_parent / "source" / "labels.json").write_bytes(b"outside")
            module = __import__(
                "content_agent.evaluation_migration", fromlist=["_copy_payload_record"]
            )
            original_copy = module._copy_payload_record
            swapped = False

            def swap_after_copy(*arguments: object) -> None:
                nonlocal swapped
                original_copy(*arguments)
                if not swapped:
                    source_parent.rename(root / "source-parent-original")
                    source_parent.symlink_to(outside_parent, target_is_directory=True)
                    swapped = True

            with patch(
                "content_agent.evaluation_migration._copy_payload_record",
                side_effect=swap_after_copy,
            ):
                with self.assertRaises(EvaluationMigrationError):
                    copy_evaluation(source, self.destination(layout), layout)

            self.assertFalse(self.destination(layout).exists())

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_destination_parent_swap_after_open_cannot_redirect_promotion(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            outside = root / "outside"
            outside.mkdir()
            module = __import__(
                "content_agent.evaluation_migration", fromlist=["_copy_payload_record"]
            )
            original_copy = module._copy_payload_record
            swapped = False

            def swap_after_copy(*arguments: object) -> None:
                nonlocal swapped
                original_copy(*arguments)
                if not swapped:
                    destination.parent.rename(destination.parent.with_name("zoro-audition-original"))
                    destination.parent.symlink_to(outside, target_is_directory=True)
                    swapped = True

            with patch(
                "content_agent.evaluation_migration._copy_payload_record",
                side_effect=swap_after_copy,
            ):
                with self.assertRaises(EvaluationMigrationError):
                    copy_evaluation(source, destination, layout)

            self.assertEqual(list(outside.rglob("*")), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_destination_parent_swap_after_receipt_write_cannot_false_success(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            outside = root / "outside"
            outside.mkdir()
            module = __import__(
                "content_agent.evaluation_migration", fromlist=["_write_durable_file"]
            )
            original_write = module._write_durable_file
            swapped = False

            def swap_after_receipt(
                directory_descriptor: int, name: str, payload: bytes
            ) -> None:
                nonlocal swapped
                original_write(directory_descriptor, name, payload)
                if name == "migration-receipt.json" and not swapped:
                    destination.parent.rename(
                        destination.parent.with_name("zoro-audition-original")
                    )
                    destination.parent.symlink_to(outside, target_is_directory=True)
                    swapped = True

            with patch(
                "content_agent.evaluation_migration._write_durable_file",
                side_effect=swap_after_receipt,
            ):
                with self.assertRaises(EvaluationMigrationError):
                    copy_evaluation(source, destination, layout)

            self.assertEqual(list(outside.rglob("*")), [])

    def test_failure_cleanup_cannot_move_private_payload_into_escaped_recovery_path(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_copy_payload_record"],
        )
        original_copy = module._copy_payload_record
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"private-evidence")
            destination = self.destination(layout)
            recovery = layout.workspace / "cache" / "evaluation-migration-recovery"
            escaped = root / "escaped-recovery"

            def escape_recovery_then_fail(*arguments: object) -> None:
                original_copy(*arguments)
                if recovery.exists():
                    recovery.rename(escaped)
                    recovery.mkdir()
                raise EvaluationMigrationError("copy failed after recovery escape")

            with patch(
                "content_agent.evaluation_migration._copy_payload_record",
                side_effect=escape_recovery_then_fail,
            ):
                with self.assertRaisesRegex(EvaluationMigrationError, "copy failed"):
                    copy_evaluation(source, destination, layout)

            escaped_payloads = (
                [path for path in escaped.rglob("*") if path.is_file()]
                if escaped.exists()
                else []
            )
            self.assertEqual(escaped_payloads, [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_idempotent_return_revalidates_canonical_parent_binding(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            copy_evaluation(source, destination, layout)
            outside = root / "outside"
            outside.mkdir()
            module = __import__(
                "content_agent.evaluation_migration",
                fromlist=["_read_existing_receipt_descriptor"],
            )
            original_read = module._read_existing_receipt_descriptor

            def swap_after_read(*arguments: object):
                receipt = original_read(*arguments)
                destination.parent.rename(
                    destination.parent.with_name("zoro-audition-original")
                )
                destination.parent.symlink_to(outside, target_is_directory=True)
                return receipt

            with patch(
                "content_agent.evaluation_migration._read_existing_receipt_descriptor",
                side_effect=swap_after_read,
            ):
                with self.assertRaises(EvaluationMigrationError):
                    copy_evaluation(source, destination, layout)

            self.assertEqual(list(outside.rglob("*")), [])

    def test_cache_and_control_files_are_excluded_from_inventory_and_destination(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "__pycache__").mkdir()
            (source / "__pycache__" / "cached.pyc").write_bytes(b"cache")
            (source / "nested").mkdir()
            (source / "nested" / "also.pyc").write_bytes(b"cache")
            (source / ".DS_Store").write_bytes(b"finder")
            (source / "payload-inventory.json").write_bytes(b"old control")
            (source / "migration-receipt.json").write_bytes(b"old receipt")
            (source / "labels.json").write_bytes(b"payload")

            receipt = copy_evaluation(source, self.destination(layout), layout)
            inventory = json.loads(receipt.inventory_path.read_text(encoding="utf-8"))

            self.assertEqual([entry["path"] for entry in inventory], ["labels.json"])
            self.assertEqual(
                set(receipt.excluded_paths),
                {
                    ".DS_Store",
                    "__pycache__/",
                    "__pycache__/cached.pyc",
                    "migration-receipt.json",
                    "nested/also.pyc",
                    "payload-inventory.json",
                },
            )
            self.assertFalse((receipt.destination / ".DS_Store").exists())
            self.assertFalse((receipt.destination / "nested" / "also.pyc").exists())
            self.assertFalse((receipt.destination / "__pycache__").exists())

    def test_historical_absolute_path_text_is_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            historical_payload = (
                b'{"run_dir":"/Users/example/Archive/old-content-agent/.skill-evals/'
                b'zoro-audition/iteration-1","note":"unchanged"}\n'
            )
            (source / "run-summary.json").write_bytes(historical_payload)

            receipt = copy_evaluation(source, self.destination(layout), layout)

            self.assertEqual(
                (receipt.destination / "run-summary.json").read_bytes(), historical_payload
            )
            self.assertEqual((source / "run-summary.json").read_bytes(), historical_payload)

    def test_source_and_destination_trees_must_be_disjoint(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, _, layout = self.create_fixture(temporary_directory)
            source_parent = layout.workspace / "evaluations"
            source_parent.mkdir(parents=True)
            (source_parent / "labels.json").write_bytes(b"source")

            cases = (
                (source_parent, source_parent),
                (source_parent, source_parent / "suite" / "iteration"),
                (source_parent / "nested", source_parent),
            )
            (source_parent / "nested").mkdir()
            (source_parent / "nested" / "labels.json").write_bytes(b"nested")
            for source, destination in cases:
                with self.subTest(source=source, destination=destination):
                    with self.assertRaisesRegex(EvaluationMigrationError, "overlap"):
                        copy_evaluation(source, destination, layout)

    def test_atomic_promotion_never_replaces_existing_empty_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory).resolve()
            source = parent / "prepared"
            destination = parent / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "payload.txt").write_text("prepared", encoding="utf-8")
            parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                with self.assertRaisesRegex(EvaluationMigrationError, "already exists"):
                    _rename_noreplace(parent_fd, source.name, destination.name)
            finally:
                os.close(parent_fd)
            self.assertTrue(source.exists())
            self.assertTrue(destination.exists())
            self.assertFalse((destination / "payload.txt").exists())

    def test_existing_migration_lock_fails_closed_without_copy(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            destination.parent.mkdir(parents=True)
            lock = destination.parent / _lock_name(destination.name)
            lock.write_text("owned elsewhere\n", encoding="utf-8")

            with self.assertRaisesRegex(EvaluationMigrationError, "lock"):
                copy_evaluation(source, destination, layout)

            self.assertFalse(destination.exists())
            self.assertEqual(lock.read_text(encoding="utf-8"), "owned elsewhere\n")

    def test_completed_destination_with_stale_lock_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            copy_evaluation(source, destination, layout)
            lock = destination.parent / _lock_name(destination.name)
            lock.write_text("stale\n", encoding="utf-8")

            with self.assertRaisesRegex(EvaluationMigrationError, "residue"):
                copy_evaluation(source, destination, layout)

            self.assertEqual(lock.read_text(encoding="utf-8"), "stale\n")

    def test_abandoned_temp_fails_closed_for_explicit_recovery(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            destination.parent.mkdir(parents=True)
            abandoned = destination.parent / (
                f"{_temp_prefix(destination.name)}deadbeef.tmp"
            )
            abandoned.mkdir()
            (abandoned / "partial.json").write_bytes(b"partial")

            with self.assertRaisesRegex(EvaluationMigrationError, "abandoned"):
                copy_evaluation(source, destination, layout)

            self.assertEqual((abandoned / "partial.json").read_bytes(), b"partial")
            self.assertFalse(destination.exists())

    def test_cleanup_failure_is_surfaced(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")

            with patch(
                "content_agent.evaluation_migration._copy_payload_record",
                side_effect=EvaluationMigrationError("copy failed"),
            ), patch(
                "content_agent.evaluation_migration._quarantine_owned_entry",
                side_effect=OSError("recovery blocked"),
            ):
                with self.assertRaisesRegex(EvaluationMigrationError, "cleanup also failed"):
                    copy_evaluation(source, self.destination(layout), layout)

    def test_cleanup_continues_to_quarantine_lock_after_tree_recovery_failure(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_OwnedEntry", "_cleanup_owned_state"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            temporary = parent / ".iteration-1.deadbeef.tmp"
            temporary.mkdir()
            lock = parent / ".iteration-1.migration.lock"
            lock.write_text("owned\n", encoding="utf-8")
            temporary_status = temporary.stat()
            lock_status = lock.stat()
            temporary_owned = module._OwnedEntry(
                temporary.name,
                (temporary_status.st_dev, temporary_status.st_ino),
            )
            lock_owned = module._OwnedEntry(
                lock.name,
                (lock_status.st_dev, lock_status.st_ino),
            )
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                with patch(
                    "content_agent.evaluation_migration._quarantine_owned_tree",
                    side_effect=OSError("tree recovery blocked"),
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError, "temporary directory cleanup failed"
                    ):
                        module._cleanup_owned_state(
                            parent_descriptor, temporary_owned, lock_owned
                        )
                self.assertTrue(temporary.exists())
                self.assertFalse(lock.exists())
                retained_locks = [
                    path
                    for path in parent.iterdir()
                    if path.name.startswith(".retained-lock-")
                    and path.name.endswith(".recovery.tmp")
                ]
                self.assertEqual(len(retained_locks), 1)
                self.assertEqual(
                    retained_locks[0].read_text(encoding="utf-8"), "owned\n"
                )
            finally:
                os.close(parent_descriptor)

    def test_promotion_fsync_failure_quarantines_published_tree_and_lock(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_sync_rename_parents"],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            original_sync = module._sync_rename_parents
            failed = False

            def fail_first_same_parent_sync(
                source_descriptor: int, destination_descriptor: int
            ) -> None:
                nonlocal failed
                if source_descriptor == destination_descriptor and not failed:
                    failed = True
                    raise OSError("promotion fsync failed")
                original_sync(source_descriptor, destination_descriptor)

            with patch(
                "content_agent.evaluation_migration._sync_rename_parents",
                side_effect=fail_first_same_parent_sync,
            ):
                with self.assertRaisesRegex(OSError, "promotion fsync failed"):
                    copy_evaluation(source, destination, layout)

            self.assertFalse(destination.exists())
            retained = list(destination.parent.iterdir())
            self.assertEqual(len(retained), 2)
            retained_tree = next(path for path in retained if path.is_dir())
            self.assertEqual((retained_tree / "labels.json").read_bytes(), b"source")
            self.assertEqual(len([path for path in retained if path.is_file()]), 1)

    def test_migration_recovery_never_calls_delete_primitives(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")

            with patch(
                "content_agent.evaluation_migration.os.unlink",
                side_effect=AssertionError("unlink must not be called"),
            ), patch(
                "content_agent.evaluation_migration.os.rmdir",
                side_effect=AssertionError("rmdir must not be called"),
            ):
                copy_evaluation(source, self.destination(layout), layout)

    def test_cleanup_never_deletes_a_replacement_lock_owner(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            _, source, layout = self.create_fixture(temporary_directory)
            (source / "labels.json").write_bytes(b"source")
            destination = self.destination(layout)
            lock = destination.parent / _lock_name(destination.name)
            original_lock_descriptor: int | None = None

            def replace_lock_then_fail(*arguments: object) -> None:
                nonlocal original_lock_descriptor
                del arguments
                original_lock_descriptor = os.open(lock, os.O_RDONLY)
                lock.unlink()
                lock.write_text("replacement owner\n", encoding="utf-8")
                raise EvaluationMigrationError("copy failed")

            try:
                with patch(
                    "content_agent.evaluation_migration._copy_payload_record",
                    side_effect=replace_lock_then_fail,
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError, "cleanup also failed"
                    ):
                        copy_evaluation(source, destination, layout)
            finally:
                if original_lock_descriptor is not None:
                    os.close(original_lock_descriptor)

            self.assertEqual(lock.read_text(encoding="utf-8"), "replacement owner\n")

    def test_copy_closes_source_parent_when_destination_parent_open_fails(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_copy_payload_record", "_open_relative_directory"],
        )
        original_open = module._open_relative_directory
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            (source / "labels.json").write_bytes(b"source")
            source_descriptor = os.open(
                source, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            destination_descriptor = os.open(
                destination, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            opened_source_parent: int | None = None
            calls = 0

            def fail_second_open(*arguments: object, **keywords: object) -> int:
                nonlocal calls, opened_source_parent
                calls += 1
                if calls == 1:
                    opened_source_parent = original_open(*arguments, **keywords)
                    return opened_source_parent
                raise EvaluationMigrationError("destination parent open failed")

            try:
                with patch(
                    "content_agent.evaluation_migration._open_relative_directory",
                    side_effect=fail_second_open,
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError, "destination parent open failed"
                    ):
                        module._copy_payload_record(
                            source_descriptor,
                            destination_descriptor,
                            {
                                "path": "labels.json",
                                "bytes": 6,
                                "sha256": hashlib.sha256(b"source").hexdigest(),
                            },
                        )
                self.assertIsNotNone(opened_source_parent)
                with self.assertRaises(OSError):
                    os.fstat(opened_source_parent)
            finally:
                os.close(destination_descriptor)
                os.close(source_descriptor)

    def test_lock_initialization_failures_quarantine_only_the_owned_lock(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_acquire_lock"],
        )
        phases = ("write", "file-fsync", "parent-fsync")
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory).resolve()
                parent_descriptor = os.open(
                    parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                original_fsync = os.fsync
                fsync_calls = 0

                def fail_selected_fsync(file_descriptor: int) -> None:
                    nonlocal fsync_calls
                    fsync_calls += 1
                    should_fail = (phase == "file-fsync" and fsync_calls == 1) or (
                        phase == "parent-fsync" and fsync_calls == 2
                    )
                    if should_fail:
                        raise OSError(f"{phase} failed")
                    original_fsync(file_descriptor)

                write_patch = (
                    patch(
                        "content_agent.evaluation_migration._write_all",
                        side_effect=OSError("write failed"),
                    )
                    if phase == "write"
                    else patch(
                        "content_agent.evaluation_migration._write_all",
                        wraps=module._write_all,
                    )
                )
                try:
                    with write_patch, patch(
                        "content_agent.evaluation_migration.os.fsync",
                        side_effect=fail_selected_fsync,
                    ):
                        with self.assertRaises(OSError):
                            module._acquire_lock(parent_descriptor, "iteration-1")
                    retained = list(parent.iterdir())
                    self.assertEqual(len(retained), 1)
                    self.assertTrue(retained[0].name.startswith(".retained-lock-"))
                    self.assertTrue(retained[0].is_file())
                finally:
                    os.close(parent_descriptor)

    def test_lock_identity_failure_closes_and_recovers_the_owned_lock(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_acquire_lock"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            original_identity = module._identity
            identity_calls = 0

            def fail_first_identity(file_descriptor: int) -> tuple[int, int]:
                nonlocal identity_calls
                identity_calls += 1
                if identity_calls == 1:
                    raise OSError("fstat failed")
                return original_identity(file_descriptor)

            try:
                with patch(
                    "content_agent.evaluation_migration._identity",
                    side_effect=fail_first_identity,
                ):
                    with self.assertRaisesRegex(OSError, "fstat failed"):
                        module._acquire_lock(parent_descriptor, "iteration-1")
                retained = list(parent.iterdir())
                self.assertEqual(len(retained), 1)
                self.assertTrue(retained[0].name.startswith(".retained-lock-"))
                self.assertTrue(retained[0].is_file())
            finally:
                os.close(parent_descriptor)

    def test_unreadable_lock_identity_closes_descriptor_and_preserves_canonical_lock(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_acquire_lock"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            captured_descriptor: int | None = None

            def fail_identity(file_descriptor: int) -> tuple[int, int]:
                nonlocal captured_descriptor
                captured_descriptor = file_descriptor
                raise OSError("persistent fstat failure")

            try:
                with patch(
                    "content_agent.evaluation_migration._identity",
                    side_effect=fail_identity,
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError, "preserved for review"
                    ):
                        module._acquire_lock(parent_descriptor, "iteration-1")
                self.assertIsNotNone(captured_descriptor)
                with self.assertRaises(OSError):
                    os.fstat(captured_descriptor)
                lock = parent / _lock_name("iteration-1")
                self.assertTrue(lock.is_file())
            finally:
                os.close(parent_descriptor)

    def test_temp_initialization_failures_quarantine_only_the_owned_directory(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_create_unique_temp"],
        )
        for phase in ("parent-fsync", "directory-open"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                parent = Path(directory).resolve()
                parent_descriptor = os.open(
                    parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                original_fsync = os.fsync
                original_open = os.open
                fsync_failed = False
                open_failed = False

                def fail_first_fsync(file_descriptor: int) -> None:
                    nonlocal fsync_failed
                    if phase == "parent-fsync" and not fsync_failed:
                        fsync_failed = True
                        raise OSError("parent fsync failed")
                    original_fsync(file_descriptor)

                def fail_first_open(*arguments: object, **keywords: object) -> int:
                    nonlocal open_failed
                    if phase == "directory-open" and not open_failed:
                        open_failed = True
                        raise OSError("temp directory open failed")
                    return original_open(*arguments, **keywords)

                try:
                    with patch(
                        "content_agent.evaluation_migration.os.fsync",
                        side_effect=fail_first_fsync,
                    ), patch(
                        "content_agent.evaluation_migration.os.open",
                        side_effect=fail_first_open,
                    ):
                        with self.assertRaises(OSError):
                            module._create_unique_temp(
                                parent_descriptor,
                                "iteration-1",
                                expected_device=os.fstat(parent_descriptor).st_dev,
                            )
                    retained = list(parent.iterdir())
                    self.assertEqual(len(retained), 1)
                    self.assertTrue(retained[0].name.startswith(".retained-tree-"))
                    self.assertTrue(retained[0].is_dir())
                finally:
                    os.close(parent_descriptor)

    def test_temp_directory_rejects_unprobed_device_before_private_copy(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_create_unique_temp", "_entry_status"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            expected_device = os.fstat(parent_descriptor).st_dev
            original_entry_status = module._entry_status

            def report_unprobed_device(
                directory_descriptor: int, name: str
            ) -> os.stat_result | None:
                status = original_entry_status(directory_descriptor, name)
                if status is None:
                    return None
                values = list(status)
                values[2] = status.st_dev + 1
                return os.stat_result(values)

            try:
                with patch(
                    "content_agent.evaluation_migration._entry_status",
                    side_effect=report_unprobed_device,
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError,
                        "filesystem changed|unprobed filesystem",
                    ):
                        module._create_unique_temp(
                            parent_descriptor,
                            "iteration-1",
                            expected_device=expected_device,
                        )

                retained = list(parent.iterdir())
                self.assertEqual(len(retained), 1)
                self.assertTrue(retained[0].is_dir())
                self.assertEqual(list(retained[0].iterdir()), [])
            finally:
                os.close(parent_descriptor)

    def test_atomic_quarantine_preserves_lock_replacement_owner(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_OwnedEntry", "_release_lock", "_rename_noreplace"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            lock = parent / ".iteration-1.migration.lock"
            lock.write_text("owned\n", encoding="utf-8")
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            status = lock.stat()
            owned = module._OwnedEntry(
                name=lock.name,
                identity=(status.st_dev, status.st_ino),
            )
            original_rename = module._rename_noreplace
            injected = False

            def inject_replacement(
                descriptor: int,
                source_name: str,
                destination_name: str,
                **keywords: object,
            ) -> None:
                nonlocal injected
                if source_name == owned.name and not injected:
                    lock.rename(parent / "owned-original.lock")
                    lock.write_text("replacement\n", encoding="utf-8")
                    injected = True
                original_rename(
                    descriptor, source_name, destination_name, **keywords
                )

            try:
                with patch(
                    "content_agent.evaluation_migration._rename_noreplace",
                    side_effect=inject_replacement,
                ):
                    with self.assertRaisesRegex(EvaluationMigrationError, "ownership changed"):
                        module._release_lock(parent_descriptor, owned)
                self.assertEqual(lock.read_text(encoding="utf-8"), "replacement\n")
                self.assertEqual(
                    (parent / "owned-original.lock").read_text(encoding="utf-8"),
                    "owned\n",
                )
            finally:
                os.close(parent_descriptor)

    def test_atomic_quarantine_preserves_directory_replacement_owner(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_OwnedEntry", "_quarantine_owned_tree", "_rename_noreplace"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            owned_path = parent / "iteration-1"
            owned_path.mkdir()
            (owned_path / "owned.txt").write_text("owned\n", encoding="utf-8")
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            status = owned_path.stat()
            owned = module._OwnedEntry(
                name=owned_path.name,
                identity=(status.st_dev, status.st_ino),
            )
            original_rename = module._rename_noreplace
            injected = False

            def inject_replacement(
                descriptor: int,
                source_name: str,
                destination_name: str,
                **keywords: object,
            ) -> None:
                nonlocal injected
                if source_name == owned.name and not injected:
                    owned_path.rename(parent / "owned-original")
                    owned_path.mkdir()
                    (owned_path / "replacement.txt").write_text(
                        "replacement\n", encoding="utf-8"
                    )
                    injected = True
                original_rename(
                    descriptor, source_name, destination_name, **keywords
                )

            try:
                with patch(
                    "content_agent.evaluation_migration._rename_noreplace",
                    side_effect=inject_replacement,
                ):
                    with self.assertRaisesRegex(EvaluationMigrationError, "ownership changed"):
                        module._quarantine_owned_tree(parent_descriptor, owned)
                self.assertEqual(
                    (owned_path / "replacement.txt").read_text(encoding="utf-8"),
                    "replacement\n",
                )
                self.assertEqual(
                    (parent / "owned-original" / "owned.txt").read_text(encoding="utf-8"),
                    "owned\n",
                )
            finally:
                os.close(parent_descriptor)

    def test_restore_sync_failure_reports_original_name_and_uncertain_durability(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_OwnedEntry", "_release_lock", "_sync_rename_parents"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            lock = parent / ".iteration-1.migration.lock"
            lock.write_text("owned\n", encoding="utf-8")
            status = lock.stat()
            owned = module._OwnedEntry(lock.name, (status.st_dev, status.st_ino))
            lock.rename(parent / "owned-original.lock")
            lock.write_text("replacement\n", encoding="utf-8")
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            original_sync = module._sync_rename_parents
            sync_calls = 0

            def fail_restore_sync(
                source_descriptor: int, destination_descriptor: int
            ) -> None:
                nonlocal sync_calls
                sync_calls += 1
                if sync_calls == 2:
                    raise OSError("restore fsync failed")
                original_sync(source_descriptor, destination_descriptor)

            try:
                with patch(
                    "content_agent.evaluation_migration._sync_rename_parents",
                    side_effect=fail_restore_sync,
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError,
                        "restored.*original name.*durability",
                    ):
                        module._release_lock(parent_descriptor, owned)
                self.assertEqual(lock.read_text(encoding="utf-8"), "replacement\n")
                self.assertEqual(
                    (parent / "owned-original.lock").read_text(encoding="utf-8"),
                    "owned\n",
                )
                retained = [
                    path
                    for path in parent.iterdir()
                    if path.name.endswith(".recovery.tmp")
                ]
                self.assertEqual(retained, [])
            finally:
                os.close(parent_descriptor)

    def test_quarantine_collision_retries_without_overwrite(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_OwnedEntry", "_release_lock"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            lock = parent / ".iteration-1.migration.lock"
            lock.write_text("owned\n", encoding="utf-8")
            owned_digest = hashlib.sha256(os.fsencode(lock.name)).hexdigest()[:16]
            collision = parent / (
                f".retained-lock-{owned_digest}-deadbeef.recovery.tmp"
            )
            collision.write_text("existing\n", encoding="utf-8")
            status = lock.stat()
            owned = module._OwnedEntry(
                lock.name,
                (status.st_dev, status.st_ino),
            )
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                with patch(
                    "content_agent.evaluation_migration.secrets.token_hex",
                    side_effect=("deadbeef", "cafebabe"),
                ):
                    retained = module._release_lock(parent_descriptor, owned)
                self.assertEqual(collision.read_text(encoding="utf-8"), "existing\n")
                self.assertEqual(
                    (parent / retained.name).read_text(encoding="utf-8"), "owned\n"
                )
            finally:
                os.close(parent_descriptor)

    def test_cross_device_quarantine_failure_preserves_source_without_copy_fallback(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_OwnedEntry", "_release_lock"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            lock = parent / ".iteration-1.migration.lock"
            lock.write_text("owned\n", encoding="utf-8")
            status = lock.stat()
            owned = module._OwnedEntry(
                lock.name,
                (status.st_dev, status.st_ino),
            )
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                with patch(
                    "content_agent.evaluation_migration._rename_noreplace",
                    side_effect=EvaluationMigrationError(
                        "atomic evaluation rename failed: Cross-device link"
                    ),
                ):
                    with self.assertRaisesRegex(
                        EvaluationMigrationError, "Cross-device"
                    ):
                        module._release_lock(parent_descriptor, owned)
                self.assertEqual(lock.read_text(encoding="utf-8"), "owned\n")
                self.assertEqual([path.name for path in parent.iterdir()], [lock.name])
            finally:
                os.close(parent_descriptor)

    def test_post_quarantine_lock_replacement_is_never_deleted(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_OwnedEntry", "_release_lock", "_quarantine_owned_entry"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            lock = parent / ".iteration-1.migration.lock"
            lock.write_text("owned\n", encoding="utf-8")
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            status = lock.stat()
            owned = module._OwnedEntry(
                name=lock.name,
                identity=(status.st_dev, status.st_ino),
            )
            original_quarantine = module._quarantine_owned_entry
            replacement_path: Path | None = None

            def replace_after_quarantine(*arguments: object, **keywords: object):
                nonlocal replacement_path
                quarantined = original_quarantine(*arguments, **keywords)
                quarantined_path = parent / quarantined.name
                quarantined_path.rename(parent / "owned-quarantined.lock")
                quarantined_path.write_text("replacement\n", encoding="utf-8")
                replacement_path = quarantined_path
                return quarantined

            try:
                with patch(
                    "content_agent.evaluation_migration._quarantine_owned_entry",
                    side_effect=replace_after_quarantine,
                ):
                    module._release_lock(parent_descriptor, owned)
                self.assertIsNotNone(replacement_path)
                self.assertEqual(
                    replacement_path.read_text(encoding="utf-8"), "replacement\n"
                )
                self.assertEqual(
                    (parent / "owned-quarantined.lock").read_text(encoding="utf-8"),
                    "owned\n",
                )
            finally:
                os.close(parent_descriptor)

    def test_post_quarantine_directory_replacement_is_never_deleted(self):
        module = __import__(
            "content_agent.evaluation_migration",
            fromlist=["_OwnedEntry", "_quarantine_owned_tree", "_quarantine_owned_entry"],
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            owned_path = parent / "iteration-1"
            owned_path.mkdir()
            (owned_path / "owned.txt").write_text("owned\n", encoding="utf-8")
            status = owned_path.stat()
            owned = module._OwnedEntry(
                name=owned_path.name,
                identity=(status.st_dev, status.st_ino),
            )
            parent_descriptor = os.open(
                parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            original_quarantine = module._quarantine_owned_entry
            replacement_path: Path | None = None

            def replace_after_quarantine(*arguments: object, **keywords: object):
                nonlocal replacement_path
                quarantined = original_quarantine(*arguments, **keywords)
                quarantined_path = parent / quarantined.name
                quarantined_path.rename(parent / "owned-quarantined")
                quarantined_path.mkdir()
                (quarantined_path / "replacement.txt").write_text(
                    "replacement\n", encoding="utf-8"
                )
                replacement_path = quarantined_path
                return quarantined

            try:
                with patch(
                    "content_agent.evaluation_migration._quarantine_owned_entry",
                    side_effect=replace_after_quarantine,
                ):
                    module._quarantine_owned_tree(parent_descriptor, owned)
                self.assertIsNotNone(replacement_path)
                self.assertEqual(
                    (replacement_path / "replacement.txt").read_text(encoding="utf-8"),
                    "replacement\n",
                )
                self.assertEqual(
                    (parent / "owned-quarantined" / "owned.txt").read_text(
                        encoding="utf-8"
                    ),
                    "owned\n",
                )
            finally:
                os.close(parent_descriptor)


class EvaluationMigrationCliTests(unittest.TestCase):
    def test_cli_rejects_suite_and_iteration_traversal_before_creating_workspace(self):
        repository = Path(__file__).parents[2]
        script = repository / "scripts" / "content_agent_cli.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "content-agent"
            root.mkdir()
            (root / "content-agent.config.json").write_text(
                '{"schema_version":1,"canonical_root_name":"content-agent","workspace":"workspace"}\n',
                encoding="utf-8",
            )
            source = root / "source"
            source.mkdir()
            (source / "labels.json").write_bytes(b"source")
            environment = {**os.environ, "PYTHONPATH": str(repository / "scripts")}

            for option, invalid_value in (("--suite-id", "../escape"), ("--iteration", "a/b")):
                with self.subTest(option=option, invalid_value=invalid_value):
                    command = [
                        sys.executable,
                        str(script),
                        "migrate-evaluation",
                        "--source",
                        "source",
                        "--suite-id",
                        "zoro-audition",
                        "--iteration",
                        "iteration-1",
                        option,
                        invalid_value,
                    ]
                    result = subprocess.run(
                        command,
                        cwd=root,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("must be a single path component", result.stdout)
                    self.assertFalse((root / "workspace").exists())


if __name__ == "__main__":
    unittest.main()
