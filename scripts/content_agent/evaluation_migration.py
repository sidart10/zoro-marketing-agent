"""Copy evaluation evidence into the private workspace with verifiable receipts.

The secure migration path requires Darwin or Linux descriptor-relative filesystem
primitives. Other platforms fail closed before creating migration state. Owned
recovery entries are retained as ignored siblings and are never deleted
by this migration command.

Capability preflight may create one ignored, zero-byte marker and then toggles
that marker between two fixed names on the destination filesystem. The marker
contains no private data and is retained instead of deleted on every outcome.
"""

import ctypes
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from content_agent.layout import ContentAgentLayout, ContentAgentLayoutError


INVENTORY_FILENAME = "payload-inventory.json"
RECEIPT_FILENAME = "migration-receipt.json"
RECEIPT_SCHEMA_VERSION = 1
_CONTROL_FILENAMES = frozenset({INVENTORY_FILENAME, RECEIPT_FILENAME})
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "destination",
        "source_count",
        "destination_count",
        "source_bytes",
        "destination_bytes",
        "inventory_sha256",
        "excluded_paths",
        "source_preserved",
        "timestamp",
    }
)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_PROBE_READ_FLAGS = _READ_FLAGS | getattr(os, "O_NONBLOCK", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
_MINIMUM_METADATA_HEADROOM = 1024 * 1024
_DARWIN_RENAME_FLAGS = 0x00000004 | 0x00000010
_LINUX_RENAME_FLAGS = 0x00000001
_NATIVE_RENAME_PROBE_NAMES = (
    ".content-agent-rename-probe-a.tmp",
    ".content-agent-rename-probe-b.tmp",
)


class EvaluationMigrationError(ContentAgentLayoutError):
    """Evaluation evidence cannot be migrated without losing its integrity."""


class _DestinationExists(EvaluationMigrationError):
    """Atomic promotion found an existing destination and did not replace it."""


@dataclass(frozen=True)
class EvaluationMigrationReceipt:
    source: Path
    destination: Path
    inventory_path: Path
    receipt_path: Path
    source_count: int
    destination_count: int
    source_bytes: int
    destination_bytes: int
    inventory_sha256: str
    excluded_paths: tuple[str, ...]
    mismatches: tuple[str, ...]
    source_preserved: bool
    timestamp: str
    idempotent: bool


@dataclass(frozen=True)
class _Inventory:
    records: tuple[dict[str, object], ...]
    excluded_paths: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.records)

    @property
    def total_bytes(self) -> int:
        return sum(int(record["bytes"]) for record in self.records)


@dataclass(frozen=True)
class _OwnedEntry:
    name: str
    identity: tuple[int, int]


@dataclass(frozen=True)
class _NativeRename:
    library: object
    function: object
    flags: int


@dataclass(frozen=True)
class _RecoveryContext:
    descriptor: int
    path: Path
    identity: tuple[int, int]
    primitive: _NativeRename


@dataclass(frozen=True)
class _ProbeFilesystemContext:
    anchor_descriptor: int
    existing_parent_descriptor: int
    remaining_components: tuple[str, ...]
    device: int


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _native_rename_primitive() -> _NativeRename:
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError as error:
        raise EvaluationMigrationError(
            "atomic no-replace rename primitive is unavailable on this platform"
        ) from error
    if sys.platform == "darwin":
        function = getattr(library, "renameatx_np", None)
        flags = _DARWIN_RENAME_FLAGS
    elif sys.platform.startswith("linux"):
        function = getattr(library, "renameat2", None)
        flags = _LINUX_RENAME_FLAGS
    else:
        function = None
        flags = 0
    if function is None:
        raise EvaluationMigrationError(
            "atomic no-replace rename primitive is unavailable on this platform"
        )
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    return _NativeRename(library=library, function=function, flags=flags)


def _require_secure_platform() -> _NativeRename:
    if not (
        sys.platform == "darwin" or sys.platform.startswith("linux")
    ) or not all(
        operation in os.supports_dir_fd
        for operation in (os.open, os.mkdir, os.stat)
    ):
        raise EvaluationMigrationError(
            "secure evaluation migration is supported only on Darwin and Linux"
        )
    return _native_rename_primitive()


def _invoke_native_rename(
    primitive: _NativeRename,
    source_parent_descriptor: int,
    source_name: str,
    destination_parent_descriptor: int,
    destination_name: str,
) -> tuple[int, int]:
    ctypes.set_errno(0)
    result = primitive.function(
        source_parent_descriptor,
        os.fsencode(source_name),
        destination_parent_descriptor,
        os.fsencode(destination_name),
        primitive.flags,
    )
    return result, ctypes.get_errno()


def _valid_probe_marker_status(status: os.stat_result) -> bool:
    return (
        stat.S_ISREG(status.st_mode)
        and stat.S_IMODE(status.st_mode) == 0o600
        and status.st_uid == os.getuid()
        and status.st_nlink == 1
        and status.st_size == 0
    )


def _probe_marker_matches(
    directory_descriptor: int,
    name: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[bool, tuple[int, int] | None]:
    descriptor: int | None = None
    try:
        status = _entry_status(directory_descriptor, name)
        if status is None or not _valid_probe_marker_status(status):
            return False, None
        descriptor = os.open(name, _PROBE_READ_FLAGS, dir_fd=directory_descriptor)
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if identity != (status.st_dev, status.st_ino):
            return False, identity
        if expected_identity is not None and identity != expected_identity:
            return False, identity
        return _valid_probe_marker_status(opened), identity
    except OSError:
        return False, None
    finally:
        _close_quietly(descriptor)


def _restore_probe_marker(
    primitive: _NativeRename,
    directory_descriptor: int,
    moved_name: str,
    original_name: str,
) -> None:
    try:
        moved_status = _entry_status(directory_descriptor, moved_name)
    except OSError as error:
        raise EvaluationMigrationError(
            "native rename probe ownership changed; the moved occupant could not be "
            f"identity-bound at {moved_name}, so its location is uncertain: {error}"
        ) from error
    if moved_status is None:
        raise EvaluationMigrationError(
            "native rename probe ownership changed; the moved occupant is no longer "
            f"available at {moved_name}, so its location is uncertain"
        )
    moved_identity = (moved_status.st_dev, moved_status.st_ino)
    result, error_number = _invoke_native_rename(
        primitive,
        directory_descriptor,
        moved_name,
        directory_descriptor,
        original_name,
    )
    if result != 0:
        raise EvaluationMigrationError(
            "native rename probe ownership changed; the no-replace restore attempt "
            "failed without overwriting its destination, and the moved occupant's "
            f"current location is uncertain: {os.strerror(error_number)}"
        )
    try:
        os.fsync(directory_descriptor)
    except OSError as error:
        raise EvaluationMigrationError(
            "native rename probe ownership changed; the restore rename completed, but "
            "directory sync failed, so durability and the moved occupant's current "
            f"location is uncertain: {error}"
        ) from error
    try:
        restored_status = _entry_status(directory_descriptor, original_name)
    except OSError as error:
        raise EvaluationMigrationError(
            "native rename probe ownership changed; restore completed and synced, "
            f"but the current location is uncertain after inspection failed: {error}"
        ) from error
    if restored_status is None or (
        restored_status.st_dev,
        restored_status.st_ino,
    ) != moved_identity:
        raise EvaluationMigrationError(
            "native rename probe ownership changed; the restored binding changed after "
            "directory sync, so the moved occupant's current location is uncertain"
        )
    raise EvaluationMigrationError(
        "native rename probe ownership changed; the replacement was restored to "
        f"its original name: {original_name}"
    )


def _probe_native_rename(primitive: _NativeRename, directory_descriptor: int) -> None:
    """Toggle one bounded, zero-byte marker to prove a real no-replace rename."""
    result, error_number = _invoke_native_rename(
        primitive,
        directory_descriptor,
        "",
        directory_descriptor,
        "",
    )
    if result == 0 or error_number != errno.ENOENT:
        raise EvaluationMigrationError(
            "atomic no-replace rename primitive is unavailable on this filesystem"
        )
    first_name, second_name = _NATIVE_RENAME_PROBE_NAMES
    first_status = _entry_status(directory_descriptor, first_name)
    second_status = _entry_status(directory_descriptor, second_name)
    if first_status is not None and second_status is not None:
        raise EvaluationMigrationError(
            "native rename probe has both marker names occupied; review is required"
        )

    source_name = first_name if second_status is None else second_name
    destination_name = second_name if source_name == first_name else first_name
    marker_descriptor: int | None = None
    try:
        if first_status is None and second_status is None:
            try:
                marker_descriptor = os.open(
                    source_name, _WRITE_FLAGS, 0o600, dir_fd=directory_descriptor
                )
            except OSError as error:
                retained, _ = _probe_marker_matches(
                    directory_descriptor, source_name
                )
                detail = (
                    f"; probe marker retained at {source_name}"
                    if retained
                    else " before a valid probe marker was established"
                )
                raise EvaluationMigrationError(
                    f"native rename probe marker creation failed{detail}: {error}"
                ) from error
            try:
                marker_status = os.fstat(marker_descriptor)
                if not _valid_probe_marker_status(marker_status):
                    raise EvaluationMigrationError(
                        "new native rename probe marker is invalid and was preserved "
                        f"for review at {source_name}"
                    )
                os.fsync(marker_descriptor)
                os.fsync(directory_descriptor)
            except OSError as error:
                raise EvaluationMigrationError(
                    "native rename probe initialization failed; probe marker retained "
                    f"at {source_name}: {error}"
                ) from error
        else:
            matches, _ = _probe_marker_matches(directory_descriptor, source_name)
            if not matches:
                raise EvaluationMigrationError(
                    "native rename probe marker is invalid and was preserved: "
                    f"{source_name}"
                )
            try:
                marker_descriptor = os.open(
                    source_name, _PROBE_READ_FLAGS, dir_fd=directory_descriptor
                )
                marker_status = os.fstat(marker_descriptor)
            except OSError as error:
                raise EvaluationMigrationError(
                    "native rename probe marker could not be identity-bound and was "
                    f"preserved at {source_name}: {error}"
                ) from error

        expected_identity = (marker_status.st_dev, marker_status.st_ino)
        matches, _ = _probe_marker_matches(
            directory_descriptor, source_name, expected_identity
        )
        if not matches:
            raise EvaluationMigrationError(
                "native rename probe marker ownership changed before rename; entries "
                "were preserved for review"
            )
        result, error_number = _invoke_native_rename(
            primitive,
            directory_descriptor,
            source_name,
            directory_descriptor,
            destination_name,
        )
        if result != 0:
            if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
                raise EvaluationMigrationError(
                    "native rename probe destination became occupied; no entry was overwritten"
                )
            raise EvaluationMigrationError(
                "atomic no-replace rename primitive is unavailable on this filesystem; "
                f"probe marker retained at {source_name}"
            )
        matches, _ = _probe_marker_matches(
            directory_descriptor, destination_name, expected_identity
        )
        if not matches:
            _restore_probe_marker(
                primitive,
                directory_descriptor,
                destination_name,
                source_name,
            )
        try:
            os.fsync(directory_descriptor)
        except OSError as error:
            raise EvaluationMigrationError(
                f"native rename probe marker moved to {destination_name}, but directory "
                f"sync failed and durability is uncertain: {error}"
            ) from error
        matches, _ = _probe_marker_matches(
            directory_descriptor, destination_name, expected_identity
        )
        if not matches:
            _restore_probe_marker(
                primitive,
                directory_descriptor,
                destination_name,
                source_name,
            )
    finally:
        _close_quietly(marker_descriptor)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _is_exact_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_excluded_file(name: str) -> bool:
    return name.endswith(".pyc") or name == ".DS_Store" or name in _CONTROL_FILENAMES


def _absolute_path(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return Path(os.path.abspath(str(candidate)))


def _identity(file_descriptor: int) -> tuple[int, int]:
    status = os.fstat(file_descriptor)
    return status.st_dev, status.st_ino


def _close_quietly(file_descriptor: int | None) -> None:
    if file_descriptor is not None:
        try:
            os.close(file_descriptor)
        except OSError:
            pass


def _open_absolute_directory_nofollow(path: Path, purpose: str) -> int:
    """Open every absolute path component with O_NOFOLLOW and return the final fd."""
    if not path.is_absolute():
        raise EvaluationMigrationError(f"{purpose} must be absolute: {path}")
    parts = path.parts
    if not parts or parts[0] != path.anchor:
        raise EvaluationMigrationError(f"invalid {purpose}: {path}")
    current = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for component in parts[1:]:
            if component in {"", ".", ".."}:
                raise EvaluationMigrationError(f"invalid {purpose} component: {component!r}")
            try:
                following = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            except OSError as error:
                raise EvaluationMigrationError(
                    f"{purpose} contains a symlink or unsafe directory component: {path}"
                ) from error
            os.close(current)
            current = following
        return current
    except BaseException:
        _close_quietly(current)
        raise


def _open_relative_directory(
    root_descriptor: int,
    components: tuple[str, ...],
    *,
    create: bool = False,
    expected_device: int | None = None,
) -> int:
    current = os.dup(root_descriptor)
    try:
        if (
            expected_device is not None
            and os.fstat(current).st_dev != expected_device
        ):
            raise EvaluationMigrationError(
                "destination filesystem changed before migration state creation"
            )
        for component in components:
            if (
                not component
                or component in {".", ".."}
                or Path(component).name != component
                or PureWindowsPath(component).name != component
            ):
                raise EvaluationMigrationError(
                    f"unsafe relative directory component: {component!r}"
                )
            try:
                following = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                    os.fsync(current)
                except FileExistsError:
                    pass
                try:
                    following = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
                except OSError as error:
                    raise EvaluationMigrationError(
                        f"destination contains a symlink or unsafe directory component: {component}"
                    ) from error
            except OSError as error:
                raise EvaluationMigrationError(
                    f"path contains a symlink or unsafe directory component: {component}"
                ) from error
            try:
                if (
                    expected_device is not None
                    and os.fstat(following).st_dev != expected_device
                ):
                    raise EvaluationMigrationError(
                        "destination filesystem changed before migration state creation"
                    )
            except BaseException:
                _close_quietly(following)
                raise
            os.close(current)
            current = following
        return current
    except BaseException:
        _close_quietly(current)
        raise


def _open_probe_filesystem_anchor(
    root_descriptor: int, components: tuple[str, ...]
) -> _ProbeFilesystemContext:
    """Hold the probed mount plus the deepest existing destination ancestor."""
    current: int | None = None
    anchor: int | None = None
    try:
        current = os.dup(root_descriptor)
        anchor = os.dup(root_descriptor)
        current_device = os.fstat(root_descriptor).st_dev
        remaining_components: tuple[str, ...] = ()
        for index, component in enumerate(components):
            if (
                not component
                or component in {".", ".."}
                or Path(component).name != component
                or PureWindowsPath(component).name != component
            ):
                raise EvaluationMigrationError(
                    f"unsafe relative directory component: {component!r}"
                )
            status = _entry_status(current, component)
            if status is None:
                remaining_components = components[index:]
                break
            if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
                raise EvaluationMigrationError(
                    f"destination contains an unsafe directory component: {component}"
                )
            following: int | None = None
            try:
                following = _open_child_directory(current, component, status)
                following_device = os.fstat(following).st_dev
                if following_device != current_device:
                    replacement_anchor = os.dup(following)
                    previous_anchor = anchor
                    anchor = replacement_anchor
                    os.close(previous_anchor)
                    current_device = following_device
                previous = current
                current = following
                following = None
                os.close(previous)
            finally:
                _close_quietly(following)
        result = _ProbeFilesystemContext(
            anchor_descriptor=anchor,
            existing_parent_descriptor=current,
            remaining_components=remaining_components,
            device=current_device,
        )
        current = None
        anchor = None
        return result
    except BaseException:
        _close_quietly(current)
        _close_quietly(anchor)
        raise


def _assert_path_binding(path: Path, expected_identity: tuple[int, int], purpose: str) -> None:
    descriptor = _open_absolute_directory_nofollow(path, purpose)
    try:
        if _identity(descriptor) != expected_identity:
            raise EvaluationMigrationError(f"{purpose} changed during evaluation migration")
    finally:
        os.close(descriptor)


def _entry_status(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _open_child_directory(parent_descriptor: int, name: str, expected: os.stat_result) -> int:
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except OSError as error:
        raise EvaluationMigrationError(
            f"symlink is not allowed in evaluation evidence: {name}"
        ) from error
    try:
        status = os.fstat(descriptor)
        if (status.st_dev, status.st_ino) != (expected.st_dev, expected.st_ino):
            raise EvaluationMigrationError(
                f"evaluation evidence changed while being inspected: {name}"
            )
    except BaseException:
        _close_quietly(descriptor)
        raise
    return descriptor


def _hash_regular_file(
    parent_descriptor: int, name: str, expected: os.stat_result
) -> tuple[int, str]:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
    except OSError as error:
        raise EvaluationMigrationError(
            f"symlink is not allowed in evaluation evidence: {name}"
        ) from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or (status.st_dev, status.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise EvaluationMigrationError(
                f"evaluation evidence changed while being inspected: {name}"
            )
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)
        final_status = os.fstat(descriptor)
        if (final_status.st_dev, final_status.st_ino, final_status.st_size) != (
            status.st_dev,
            status.st_ino,
            byte_count,
        ):
            raise EvaluationMigrationError(
                f"evaluation evidence changed while being inspected: {name}"
            )
        return byte_count, digest.hexdigest()
    finally:
        os.close(descriptor)


def _inventory_descriptor(directory_descriptor: int) -> _Inventory:
    """Inventory regular payload files without following any directory entry."""
    records: list[dict[str, object]] = []
    excluded_paths: set[str] = set()

    def inspect_excluded(current_descriptor: int, relative_directory: tuple[str, ...]) -> None:
        for name in sorted(os.listdir(current_descriptor)):
            relative = (*relative_directory, name)
            display = "/".join(relative)
            status = _entry_status(current_descriptor, name)
            if status is None:
                raise EvaluationMigrationError(
                    f"evaluation evidence changed while being inspected: {display}"
                )
            if stat.S_ISLNK(status.st_mode):
                raise EvaluationMigrationError(
                    f"symlink is not allowed in evaluation evidence: {display}"
                )
            if stat.S_ISDIR(status.st_mode):
                excluded_paths.add(f"{display}/")
                child = _open_child_directory(current_descriptor, name, status)
                try:
                    inspect_excluded(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(status.st_mode):
                excluded_paths.add(display)
            else:
                raise EvaluationMigrationError(f"unsupported non-regular source path: {display}")

    def visit(current_descriptor: int, relative_directory: tuple[str, ...]) -> None:
        for name in sorted(os.listdir(current_descriptor)):
            relative = (*relative_directory, name)
            display = "/".join(relative)
            status = _entry_status(current_descriptor, name)
            if status is None:
                raise EvaluationMigrationError(
                    f"evaluation evidence changed while being inspected: {display}"
                )
            if stat.S_ISLNK(status.st_mode):
                raise EvaluationMigrationError(
                    f"symlink is not allowed in evaluation evidence: {display}"
                )
            if stat.S_ISDIR(status.st_mode):
                child = _open_child_directory(current_descriptor, name, status)
                try:
                    if name == "__pycache__":
                        excluded_paths.add(f"{display}/")
                        inspect_excluded(child, relative)
                    else:
                        visit(child, relative)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(status.st_mode):
                raise EvaluationMigrationError(f"unsupported non-regular source path: {display}")
            if _is_excluded_file(name):
                excluded_paths.add(display)
                continue
            byte_count, digest = _hash_regular_file(current_descriptor, name, status)
            records.append({"path": display, "bytes": byte_count, "sha256": digest})

    visit(directory_descriptor, ())
    records.sort(key=lambda record: str(record["path"]))
    return _Inventory(tuple(records), tuple(sorted(excluded_paths)))


def _source_path_and_descriptor(source: Path) -> tuple[Path, int]:
    candidate = _absolute_path(source)
    descriptor = _open_absolute_directory_nofollow(candidate, "evaluation source")
    return candidate, descriptor


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _require_copy_headroom(
    destination: Path,
    source_bytes: int,
    inventory_bytes: int = 0,
    payload_count: int = 0,
) -> None:
    current = destination.parent
    while not current.exists():
        if current == current.parent:
            raise EvaluationMigrationError(f"cannot find destination filesystem: {destination}")
        current = current.parent
    if not current.is_dir():
        raise EvaluationMigrationError(f"destination parent is not a directory: {current}")
    available = shutil.disk_usage(current).free
    metadata_margin = max(
        _MINIMUM_METADATA_HEADROOM,
        inventory_bytes * 2 + (payload_count + 8) * 4096,
    )
    required = source_bytes * 2 + metadata_margin
    if available < required:
        raise EvaluationMigrationError(
            f"insufficient free space for evaluation copy: need {required} bytes, have {available}"
        )


def _write_all(file_descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(file_descriptor, view)
        if written <= 0:
            raise EvaluationMigrationError("short write while persisting evaluation evidence")
        view = view[written:]


def _write_durable_file(directory_descriptor: int, name: str, payload: bytes) -> None:
    try:
        descriptor = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=directory_descriptor)
    except OSError as error:
        raise EvaluationMigrationError(f"cannot create evaluation control file: {name}") from error
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(directory_descriptor)


def _read_regular_file(directory_descriptor: int, name: str) -> bytes:
    status = _entry_status(directory_descriptor, name)
    if status is None or not stat.S_ISREG(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise EvaluationMigrationError("existing destination does not match source evaluation")
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_descriptor)
    except OSError as error:
        raise EvaluationMigrationError(
            "existing destination does not match source evaluation"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (status.st_dev, status.st_ino):
            raise EvaluationMigrationError("existing destination does not match source evaluation")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _copy_payload_record(
    source_descriptor: int,
    destination_descriptor: int,
    record: dict[str, object],
) -> None:
    relative_parts = tuple(str(record["path"]).split("/"))
    source_parent: int | None = None
    destination_parent: int | None = None
    source_file: int | None = None
    destination_file: int | None = None
    try:
        source_parent = _open_relative_directory(
            source_descriptor, relative_parts[:-1]
        )
        destination_parent = _open_relative_directory(
            destination_descriptor,
            relative_parts[:-1],
            create=True,
            expected_device=_identity(destination_descriptor)[0],
        )
        source_status = _entry_status(source_parent, relative_parts[-1])
        if source_status is None or not stat.S_ISREG(source_status.st_mode) or stat.S_ISLNK(
            source_status.st_mode
        ):
            raise EvaluationMigrationError(
                f"evaluation source changed before copy: {record['path']}"
            )
        source_file = os.open(relative_parts[-1], _READ_FLAGS, dir_fd=source_parent)
        opened_source = os.fstat(source_file)
        if (opened_source.st_dev, opened_source.st_ino) != (
            source_status.st_dev,
            source_status.st_ino,
        ):
            raise EvaluationMigrationError(
                f"evaluation source changed before copy: {record['path']}"
            )
        destination_file = os.open(
            relative_parts[-1], _WRITE_FLAGS, 0o600, dir_fd=destination_parent
        )
        digest = hashlib.sha256()
        byte_count = 0
        while True:
            chunk = os.read(source_file, 1024 * 1024)
            if not chunk:
                break
            _write_all(destination_file, chunk)
            digest.update(chunk)
            byte_count += len(chunk)
        os.fsync(destination_file)
        if byte_count != record["bytes"] or digest.hexdigest() != record["sha256"]:
            raise EvaluationMigrationError(
                f"evaluation source changed during copy: {record['path']}"
            )
        final_source = os.fstat(source_file)
        if (final_source.st_dev, final_source.st_ino, final_source.st_size) != (
            opened_source.st_dev,
            opened_source.st_ino,
            byte_count,
        ):
            raise EvaluationMigrationError(
                f"evaluation source changed during copy: {record['path']}"
            )
        os.fsync(destination_parent)
    finally:
        _close_quietly(destination_file)
        _close_quietly(source_file)
        _close_quietly(destination_parent)
        _close_quietly(source_parent)


def _mismatches(source: _Inventory, destination: _Inventory) -> tuple[str, ...]:
    source_records = {str(record["path"]): record for record in source.records}
    destination_records = {str(record["path"]): record for record in destination.records}
    return tuple(
        path
        for path in sorted(set(source_records) | set(destination_records))
        if source_records.get(path) != destination_records.get(path)
    )


def _expected_receipt(
    source: Path,
    destination: Path,
    source_inventory: _Inventory,
    destination_inventory: _Inventory,
    inventory_sha256: str,
    timestamp: str,
) -> dict[str, object]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "source": str(source),
        "destination": str(destination),
        "source_count": source_inventory.count,
        "destination_count": destination_inventory.count,
        "source_bytes": source_inventory.total_bytes,
        "destination_bytes": destination_inventory.total_bytes,
        "inventory_sha256": inventory_sha256,
        "excluded_paths": list(source_inventory.excluded_paths),
        "source_preserved": True,
        "timestamp": timestamp,
    }


def _read_existing_receipt_descriptor(
    destination_descriptor: int,
    destination: Path,
    source: Path,
    source_inventory: _Inventory,
) -> EvaluationMigrationReceipt:
    destination_inventory = _inventory_descriptor(destination_descriptor)
    mismatches = _mismatches(source_inventory, destination_inventory)
    inventory_bytes = _canonical_json(list(source_inventory.records))
    inventory_sha256 = hashlib.sha256(inventory_bytes).hexdigest()
    expected_destination_exclusions = tuple(sorted(_CONTROL_FILENAMES))
    if mismatches or destination_inventory.excluded_paths != expected_destination_exclusions:
        raise EvaluationMigrationError("existing destination does not match source evaluation")
    if _read_regular_file(destination_descriptor, INVENTORY_FILENAME) != inventory_bytes:
        raise EvaluationMigrationError("existing destination does not match source evaluation")
    receipt_bytes = _read_regular_file(destination_descriptor, RECEIPT_FILENAME)
    try:
        receipt_data = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise EvaluationMigrationError(
            "existing destination does not match source evaluation"
        ) from error
    if not isinstance(receipt_data, dict) or set(receipt_data) != _RECEIPT_FIELDS:
        raise EvaluationMigrationError("existing destination does not match source evaluation")
    if receipt_bytes != _canonical_json(receipt_data) or not _valid_timestamp(
        receipt_data.get("timestamp")
    ):
        raise EvaluationMigrationError("existing destination does not match source evaluation")
    expected = _expected_receipt(
        source,
        destination,
        source_inventory,
        destination_inventory,
        inventory_sha256,
        str(receipt_data["timestamp"]),
    )
    if receipt_data != expected:
        raise EvaluationMigrationError("existing destination does not match source evaluation")
    for field in (
        "schema_version",
        "source_count",
        "destination_count",
        "source_bytes",
        "destination_bytes",
    ):
        if not _is_exact_int(receipt_data[field]):
            raise EvaluationMigrationError("existing destination does not match source evaluation")
    if not isinstance(receipt_data["excluded_paths"], list) or not all(
        isinstance(path, str) for path in receipt_data["excluded_paths"]
    ):
        raise EvaluationMigrationError("existing destination does not match source evaluation")
    if receipt_data["source_preserved"] is not True:
        raise EvaluationMigrationError("existing destination does not match source evaluation")
    return EvaluationMigrationReceipt(
        source=source,
        destination=destination,
        inventory_path=destination / INVENTORY_FILENAME,
        receipt_path=destination / RECEIPT_FILENAME,
        source_count=source_inventory.count,
        destination_count=destination_inventory.count,
        source_bytes=source_inventory.total_bytes,
        destination_bytes=destination_inventory.total_bytes,
        inventory_sha256=inventory_sha256,
        excluded_paths=source_inventory.excluded_paths,
        mismatches=(),
        source_preserved=True,
        timestamp=str(receipt_data["timestamp"]),
        idempotent=True,
    )


def _open_existing_destination(parent_descriptor: int, name: str) -> int | None:
    status = _entry_status(parent_descriptor, name)
    if status is None:
        return None
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        raise EvaluationMigrationError("existing destination does not match source evaluation")
    return _open_child_directory(parent_descriptor, name, status)


def _rename_noreplace(
    source_parent_descriptor: int,
    source_name: str,
    destination_name: str,
    *,
    destination_parent_descriptor: int | None = None,
    primitive: _NativeRename | None = None,
) -> None:
    """Atomically rename one entry without replacing a destination entry."""
    destination_descriptor = (
        source_parent_descriptor
        if destination_parent_descriptor is None
        else destination_parent_descriptor
    )
    native = _native_rename_primitive() if primitive is None else primitive
    result, error_number = _invoke_native_rename(
        native,
        source_parent_descriptor,
        source_name,
        destination_descriptor,
        destination_name,
    )
    if result != 0:
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise _DestinationExists(
                f"evaluation destination already exists: {destination_name}"
            )
        raise EvaluationMigrationError(
            f"atomic evaluation rename failed: {os.strerror(error_number)}"
        )


def _sync_rename_parents(
    source_parent_descriptor: int, destination_parent_descriptor: int
) -> None:
    os.fsync(source_parent_descriptor)
    if destination_parent_descriptor != source_parent_descriptor:
        os.fsync(destination_parent_descriptor)


def _state_key(destination_name: str) -> str:
    return hashlib.sha256(os.fsencode(destination_name)).hexdigest()[:32]


def _lock_name(destination_name: str) -> str:
    return f".evaluation-{_state_key(destination_name)}.migration.lock"


def _temp_prefix(destination_name: str) -> str:
    return f".evaluation-{_state_key(destination_name)}."


def _migration_residue(parent_descriptor: int, destination_name: str) -> tuple[str, ...]:
    lock_name = _lock_name(destination_name)
    return tuple(
        sorted(
            name
            for name in os.listdir(parent_descriptor)
            if name == lock_name
            or (
                name.startswith(_temp_prefix(destination_name))
                and name.endswith(".tmp")
            )
        )
    )


def _acquire_lock(
    parent_descriptor: int,
    destination_name: str,
    *,
    recovery: _RecoveryContext | None = None,
) -> _OwnedEntry:
    lock_name = _lock_name(destination_name)
    abandoned = sorted(
        name
        for name in os.listdir(parent_descriptor)
        if name.startswith(_temp_prefix(destination_name)) and name.endswith(".tmp")
    )
    if abandoned:
        raise EvaluationMigrationError(
            "abandoned evaluation migration state requires review: " + ", ".join(abandoned)
        )
    descriptor: int | None = None
    owned_lock: _OwnedEntry | None = None
    try:
        try:
            descriptor = os.open(
                lock_name, _WRITE_FLAGS, 0o600, dir_fd=parent_descriptor
            )
        except FileExistsError as error:
            raise EvaluationMigrationError(
                f"evaluation migration lock already exists: {lock_name}"
            ) from error
        owned_lock = _OwnedEntry(name=lock_name, identity=_identity(descriptor))
        _write_all(
            descriptor,
            _canonical_json({"pid": os.getpid(), "created_at": _timestamp()}),
        )
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.fsync(parent_descriptor)
        return owned_lock
    except BaseException as original_error:
        created_descriptor = descriptor
        if created_descriptor is not None and owned_lock is None:
            try:
                owned_lock = _OwnedEntry(
                    name=lock_name, identity=_identity(created_descriptor)
                )
            except BaseException:
                pass
        _close_quietly(descriptor)
        if owned_lock is not None:
            try:
                _release_lock(parent_descriptor, owned_lock, recovery=recovery)
            except BaseException as recovery_error:
                raise EvaluationMigrationError(
                    "migration lock initialization failed and recovery also failed: "
                    f"{recovery_error}"
                ) from original_error
        elif created_descriptor is not None:
            raise EvaluationMigrationError(
                "migration lock initialization failed; ownership could not be proven, "
                f"so the lock was preserved for review: {lock_name}"
            ) from original_error
        raise


def _quarantine_owned_entry(
    parent_descriptor: int,
    owned: _OwnedEntry,
    purpose: str,
    *,
    recovery: _RecoveryContext | None = None,
) -> _OwnedEntry:
    recovery_descriptor = (
        parent_descriptor if recovery is None else recovery.descriptor
    )
    primitive = _native_rename_primitive() if recovery is None else recovery.primitive
    recovery_path = Path(".") if recovery is None else recovery.path
    for _ in range(32):
        retained_kind = "lock" if "lock" in purpose else "tree"
        owned_digest = hashlib.sha256(os.fsencode(owned.name)).hexdigest()[:16]
        quarantine_name = (
            f".retained-{retained_kind}-{owned_digest}-"
            f"{secrets.token_hex(8)}.recovery.tmp"
        )
        try:
            _rename_noreplace(
                parent_descriptor,
                owned.name,
                quarantine_name,
                destination_parent_descriptor=recovery_descriptor,
                primitive=primitive,
            )
        except _DestinationExists:
            continue
        moved = _OwnedEntry(name=quarantine_name, identity=owned.identity)
        try:
            _sync_rename_parents(parent_descriptor, recovery_descriptor)
        except OSError as sync_error:
            raise EvaluationMigrationError(
                f"{purpose} was retained at {recovery_path / quarantine_name}, "
                f"but directory sync failed: {sync_error}"
            ) from sync_error
        status = _entry_status(recovery_descriptor, quarantine_name)
        if status is None:
            raise EvaluationMigrationError(
                f"{purpose} disappeared during recovery: {owned.name}"
            )
        observed_identity = (status.st_dev, status.st_ino)
        if observed_identity != owned.identity:
            try:
                _rename_noreplace(
                    recovery_descriptor,
                    quarantine_name,
                    owned.name,
                    destination_parent_descriptor=parent_descriptor,
                    primitive=primitive,
                )
            except BaseException as restore_error:
                raise EvaluationMigrationError(
                    f"{purpose} ownership changed; replacement preserved at "
                    f"{recovery_path / quarantine_name}, but its original name "
                    "could not be restored: "
                    f"{restore_error}"
                ) from restore_error
            try:
                _sync_rename_parents(recovery_descriptor, parent_descriptor)
            except OSError as sync_error:
                raise EvaluationMigrationError(
                    f"{purpose} ownership changed; replacement was restored to its "
                    f"original name at {recovery_path / owned.name}, but directory "
                    f"sync failed and durability is uncertain: {sync_error}"
                ) from sync_error
            raise EvaluationMigrationError(
                f"{purpose} ownership changed; replacement was restored to its "
                f"original name: {owned.name}"
            )
        return moved
    raise EvaluationMigrationError(f"cannot quarantine {purpose} safely: {owned.name}")


def _release_lock(
    parent_descriptor: int,
    owned_lock: _OwnedEntry,
    *,
    recovery: _RecoveryContext | None = None,
) -> _OwnedEntry:
    return _quarantine_owned_entry(
        parent_descriptor,
        owned_lock,
        "evaluation migration lock",
        recovery=recovery,
    )


def _quarantine_owned_tree(
    parent_descriptor: int,
    owned: _OwnedEntry,
    *,
    recovery: _RecoveryContext | None = None,
) -> _OwnedEntry:
    return _quarantine_owned_entry(
        parent_descriptor,
        owned,
        "evaluation migration directory",
        recovery=recovery,
    )


def _create_unique_temp(
    parent_descriptor: int,
    destination_name: str,
    *,
    expected_device: int,
    recovery: _RecoveryContext | None = None,
) -> tuple[_OwnedEntry, int]:
    if _identity(parent_descriptor)[0] != expected_device:
        raise EvaluationMigrationError(
            "destination filesystem changed before temporary directory creation"
        )
    for _ in range(32):
        name = f"{_temp_prefix(destination_name)}{secrets.token_hex(8)}.tmp"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
        owned: _OwnedEntry | None = None
        descriptor: int | None = None
        try:
            status = _entry_status(parent_descriptor, name)
            if status is None or not stat.S_ISDIR(status.st_mode):
                raise EvaluationMigrationError(
                    f"new evaluation temporary directory is unavailable: {name}"
                )
            if status.st_dev != expected_device:
                raise EvaluationMigrationError(
                    "destination filesystem changed before private evaluation copy"
                )
            owned = _OwnedEntry(name=name, identity=(status.st_dev, status.st_ino))
            os.fsync(parent_descriptor)
            descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
            if _identity(descriptor) != owned.identity:
                raise EvaluationMigrationError(
                    f"evaluation temporary directory ownership changed: {name}"
                )
            return owned, descriptor
        except BaseException as original_error:
            _close_quietly(descriptor)
            if owned is not None:
                try:
                    _quarantine_owned_tree(parent_descriptor, owned, recovery=recovery)
                except BaseException as recovery_error:
                    raise EvaluationMigrationError(
                        "temporary directory initialization failed and recovery also failed: "
                        f"{recovery_error}"
                    ) from original_error
            raise
    raise EvaluationMigrationError("cannot allocate a unique evaluation migration directory")


def _cleanup_owned_state(
    parent_descriptor: int,
    temporary: _OwnedEntry | None,
    owned_lock: _OwnedEntry | None,
    published: _OwnedEntry | None = None,
    *,
    recovery: _RecoveryContext | None = None,
) -> None:
    errors: list[str] = []
    if temporary is not None:
        try:
            _quarantine_owned_tree(parent_descriptor, temporary, recovery=recovery)
        except (OSError, EvaluationMigrationError) as error:
            errors.append(f"temporary directory cleanup failed: {error}")
    if published is not None:
        try:
            _quarantine_owned_tree(parent_descriptor, published, recovery=recovery)
        except (OSError, EvaluationMigrationError) as error:
            errors.append(f"published directory cleanup failed: {error}")
    if owned_lock is not None:
        try:
            _release_lock(parent_descriptor, owned_lock, recovery=recovery)
        except (OSError, EvaluationMigrationError) as error:
            errors.append(f"migration lock cleanup failed: {error}")
    if errors:
        raise EvaluationMigrationError("; ".join(errors))


def _assert_success_bindings(
    source_path: Path,
    source_identity: tuple[int, int],
    root_path: Path,
    root_identity: tuple[int, int],
    parent_path: Path,
    parent_identity: tuple[int, int],
    destination_path: Path,
    destination_identity: tuple[int, int],
) -> None:
    _assert_path_binding(source_path, source_identity, "evaluation source")
    _assert_path_binding(root_path, root_identity, "content-agent root")
    _assert_path_binding(
        parent_path, parent_identity, "evaluation destination parent"
    )
    _assert_path_binding(
        destination_path, destination_identity, "evaluation destination"
    )


def copy_evaluation(
    source: Path, destination: Path, layout: ContentAgentLayout
) -> EvaluationMigrationReceipt:
    """Copy immutable evaluation evidence after proving its payload inventory matches."""
    primitive = _require_secure_platform()
    destination_path = _absolute_path(destination)
    confined_destination = layout.require_private_path(destination_path, "evaluation destination")
    if confined_destination != destination_path:
        raise EvaluationMigrationError(
            f"evaluation destination contains a symlink or unstable component: {destination_path}"
        )
    source_path, source_descriptor = _source_path_and_descriptor(source)
    source_identity = _identity(source_descriptor)
    root_descriptor: int | None = None
    parent_descriptor: int | None = None
    recovery: _RecoveryContext | None = None
    temporary_descriptor: int | None = None
    temporary: _OwnedEntry | None = None
    owned_lock: _OwnedEntry | None = None
    published: _OwnedEntry | None = None
    try:
        if _paths_overlap(source_path, destination_path):
            raise EvaluationMigrationError("evaluation source and destination overlap")
        source_inventory = _inventory_descriptor(source_descriptor)
        inventory_bytes = _canonical_json(list(source_inventory.records))
        _require_copy_headroom(
            destination_path,
            source_inventory.total_bytes,
            len(inventory_bytes),
            source_inventory.count,
        )

        root_path = _absolute_path(layout.root)
        root_descriptor = _open_absolute_directory_nofollow(root_path, "content-agent root")
        root_identity = _identity(root_descriptor)
        try:
            parent_relative = destination_path.parent.relative_to(root_path)
        except ValueError as error:
            raise EvaluationMigrationError(
                "evaluation destination escapes content-agent root"
            ) from error
        probe_context = _open_probe_filesystem_anchor(
            root_descriptor, tuple(parent_relative.parts)
        )
        try:
            _probe_native_rename(primitive, probe_context.anchor_descriptor)
            parent_descriptor = _open_relative_directory(
                probe_context.existing_parent_descriptor,
                probe_context.remaining_components,
                create=True,
                expected_device=probe_context.device,
            )
            parent_identity = _identity(parent_descriptor)
            if parent_identity[0] != probe_context.device:
                raise EvaluationMigrationError(
                    "destination filesystem changed after native rename preflight"
                )
        finally:
            _close_quietly(probe_context.existing_parent_descriptor)
            _close_quietly(probe_context.anchor_descriptor)
        destination_name = destination_path.name

        existing_descriptor = _open_existing_destination(parent_descriptor, destination_name)
        if existing_descriptor is not None:
            try:
                existing_identity = _identity(existing_descriptor)
                receipt = _read_existing_receipt_descriptor(
                    existing_descriptor,
                    destination_path,
                    source_path,
                    source_inventory,
                )
            finally:
                os.close(existing_descriptor)
            residue = _migration_residue(parent_descriptor, destination_name)
            if residue:
                raise EvaluationMigrationError(
                    "completed evaluation has migration residue requiring review: "
                    + ", ".join(residue)
                )
            _assert_success_bindings(
                source_path,
                source_identity,
                root_path,
                root_identity,
                destination_path.parent,
                parent_identity,
                destination_path,
                existing_identity,
            )
            return receipt

        recovery = _RecoveryContext(
            descriptor=parent_descriptor,
            path=destination_path.parent,
            identity=parent_identity,
            primitive=primitive,
        )

        owned_lock = _acquire_lock(
            parent_descriptor, destination_name, recovery=recovery
        )
        temporary, temporary_descriptor = _create_unique_temp(
            parent_descriptor,
            destination_name,
            expected_device=parent_identity[0],
            recovery=recovery,
        )
        for record in source_inventory.records:
            _copy_payload_record(source_descriptor, temporary_descriptor, record)

        _write_durable_file(temporary_descriptor, INVENTORY_FILENAME, inventory_bytes)
        destination_inventory = _inventory_descriptor(temporary_descriptor)
        mismatches = _mismatches(source_inventory, destination_inventory)
        if mismatches or destination_inventory.excluded_paths != (INVENTORY_FILENAME,):
            raise EvaluationMigrationError(
                "copied destination does not match source evaluation: " + ", ".join(mismatches)
            )
        second_source_inventory = _inventory_descriptor(source_descriptor)
        if second_source_inventory != source_inventory:
            raise EvaluationMigrationError("evaluation source changed during migration")
        _assert_path_binding(source_path, source_identity, "evaluation source")
        _assert_path_binding(root_path, root_identity, "content-agent root")
        _assert_path_binding(
            destination_path.parent,
            parent_identity,
            "evaluation destination parent",
        )

        inventory_sha256 = hashlib.sha256(inventory_bytes).hexdigest()
        timestamp = _timestamp()
        receipt_data = _expected_receipt(
            source_path,
            destination_path,
            source_inventory,
            destination_inventory,
            inventory_sha256,
            timestamp,
        )
        _write_durable_file(
            temporary_descriptor,
            RECEIPT_FILENAME,
            _canonical_json(receipt_data),
        )
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = None
        try:
            _rename_noreplace(
                parent_descriptor,
                temporary.name,
                destination_name,
                primitive=primitive,
            )
        except _DestinationExists:
            _cleanup_owned_state(
                parent_descriptor,
                temporary,
                owned_lock,
                recovery=recovery,
            )
            temporary = None
            owned_lock = None
            winner = _open_existing_destination(parent_descriptor, destination_name)
            if winner is None:
                raise EvaluationMigrationError(
                    "evaluation destination appeared during promotion but is unavailable"
                )
            try:
                winner_identity = _identity(winner)
                winner_receipt = _read_existing_receipt_descriptor(
                    winner,
                    destination_path,
                    source_path,
                    source_inventory,
                )
            finally:
                os.close(winner)
            _assert_success_bindings(
                source_path,
                source_identity,
                root_path,
                root_identity,
                destination_path.parent,
                parent_identity,
                destination_path,
                winner_identity,
            )
            return winner_receipt
        published = _OwnedEntry(
            name=destination_name,
            identity=temporary.identity,
        )
        temporary = None
        _sync_rename_parents(parent_descriptor, parent_descriptor)
        anchored_destination = _open_existing_destination(
            parent_descriptor, destination_name
        )
        if anchored_destination is None:
            raise EvaluationMigrationError(
                "promoted evaluation destination is unavailable"
            )
        try:
            if _identity(anchored_destination) != published.identity:
                raise EvaluationMigrationError(
                    "promoted evaluation destination ownership changed"
                )
        finally:
            os.close(anchored_destination)
        _assert_success_bindings(
            source_path,
            source_identity,
            root_path,
            root_identity,
            destination_path.parent,
            parent_identity,
            destination_path,
            published.identity,
        )
        try:
            _assert_path_binding(
                recovery.path,
                recovery.identity,
                "evaluation migration recovery directory",
            )
            _release_lock(parent_descriptor, owned_lock, recovery=recovery)
            _assert_path_binding(
                recovery.path,
                recovery.identity,
                "evaluation migration recovery directory",
            )
        except BaseException:
            published = None
            raise
        owned_lock = None
        result = EvaluationMigrationReceipt(
            source=source_path,
            destination=destination_path,
            inventory_path=destination_path / INVENTORY_FILENAME,
            receipt_path=destination_path / RECEIPT_FILENAME,
            source_count=source_inventory.count,
            destination_count=destination_inventory.count,
            source_bytes=source_inventory.total_bytes,
            destination_bytes=destination_inventory.total_bytes,
            inventory_sha256=inventory_sha256,
            excluded_paths=source_inventory.excluded_paths,
            mismatches=(),
            source_preserved=True,
            timestamp=timestamp,
            idempotent=False,
        )
        _assert_success_bindings(
            source_path,
            source_identity,
            root_path,
            root_identity,
            destination_path.parent,
            parent_identity,
            destination_path,
            published.identity,
        )
        published = None
        return result
    except BaseException as original_error:
        _close_quietly(temporary_descriptor)
        if parent_descriptor is not None and any(
            entry is not None for entry in (temporary, owned_lock, published)
        ):
            try:
                _cleanup_owned_state(
                    parent_descriptor,
                    temporary,
                    owned_lock,
                    published,
                    recovery=recovery,
                )
            except EvaluationMigrationError as cleanup_error:
                raise EvaluationMigrationError(
                    f"evaluation migration failed and cleanup also failed: {cleanup_error}"
                ) from original_error
        raise
    finally:
        _close_quietly(parent_descriptor)
        _close_quietly(root_descriptor)
        os.close(source_descriptor)


def require_path_component(value: str, purpose: str) -> str:
    """Validate suite and iteration names before they are joined to the workspace."""
    if not isinstance(value, str) or not value:
        raise EvaluationMigrationError(f"{purpose} must be a single path component")
    native_path = Path(value)
    windows_path = PureWindowsPath(value)
    if (
        native_path.is_absolute()
        or windows_path.is_absolute()
        or len(native_path.parts) != 1
        or len(windows_path.parts) != 1
        or value in {".", ".."}
    ):
        raise EvaluationMigrationError(f"{purpose} must be a single path component")
    return value
