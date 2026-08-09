"""A local artifact store with attempt-scoped path validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4


class ArtifactStoreError(ValueError):
    """Base exception for artifact-store validation failures."""


class UnsafeArtifactPath(ArtifactStoreError):
    """A source path or artifact URI crosses a storage boundary."""


class ArtifactTooLarge(ArtifactStoreError):
    """An artifact exceeded the configured per-file capacity."""


class ArtifactNotFound(ArtifactStoreError):
    """A syntactically valid artifact URI did not resolve to a regular file."""


@dataclass(frozen=True)
class ArtifactMetadata:
    """Caller-supplied, non-path metadata for a single attempt artifact."""

    run_id: UUID | str
    attempt_id: UUID | str
    artifact_type: str
    mime_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", UUID(str(self.run_id)))
        object.__setattr__(self, "attempt_id", UUID(str(self.attempt_id)))
        if not self.artifact_type.strip():
            raise ValueError("artifact_type must not be blank")


@dataclass(frozen=True)
class StoredArtifact:
    """Opaque artifact reference and metadata safe to persist in the database."""

    uri: str
    artifact_type: str
    attempt_id: UUID
    checksum: str
    size_bytes: int
    mime_type: str | None
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class FileArtifactStore:
    """Copies workspace files into a service-owned namespace outside runner control.

    The configured root must not overlap an attempt workspace. Runners may write
    their own workspace, but never the artifact tree; that ownership boundary is
    what prevents a runner from replacing generated destination directories while
    the store performs its atomic rename.
    """

    _CHUNK_SIZE = 1024 * 1024

    def __init__(self, root: Path, *, max_file_bytes: int = 50 * 1024 * 1024) -> None:
        if max_file_bytes < 0:
            raise ValueError("max_file_bytes must be non-negative")
        self._root = Path(root)
        self._max_file_bytes = max_file_bytes

    def put(
        self,
        source_path: Path,
        metadata: ArtifactMetadata,
        *,
        attempt_workspace: Path,
    ) -> StoredArtifact:
        """Copy a regular workspace file atomically and return a path-free reference."""
        workspace = self._validated_workspace(attempt_workspace)
        source = self._validated_source(Path(source_path), workspace)
        root = self._validated_root()
        self._ensure_separate_storage_root(root, workspace)

        relative_uri = Path(
            "runs",
            str(metadata.run_id),
            str(metadata.attempt_id),
            uuid4().hex,
        )
        destination = root / relative_uri
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._validated_destination_parent(destination.parent, root)

        temporary_path: Path | None = None
        digest = hashlib.sha256()
        size_bytes = 0
        try:
            with self._open_regular_source(source) as source_file, tempfile.NamedTemporaryFile(
                mode="xb", dir=destination.parent, prefix=".artifact-", delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                while chunk := source_file.read(self._CHUNK_SIZE):
                    size_bytes += len(chunk)
                    if size_bytes > self._max_file_bytes:
                        raise ArtifactTooLarge(
                            f"artifact exceeds {self._max_file_bytes} byte limit"
                        )
                    digest.update(chunk)
                    temporary_file.write(chunk)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            self._validated_destination_parent(destination.parent, root)
            temporary_path.replace(destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

        checksum = digest.hexdigest()
        safe_metadata = {
            "artifact_type": metadata.artifact_type,
            "attempt_id": str(metadata.attempt_id),
            "checksum": checksum,
            "size_bytes": size_bytes,
            "mime_type": metadata.mime_type,
        }
        return StoredArtifact(
            uri=relative_uri.as_posix(),
            artifact_type=metadata.artifact_type,
            attempt_id=metadata.attempt_id,
            checksum=checksum,
            size_bytes=size_bytes,
            mime_type=metadata.mime_type,
            metadata=safe_metadata,
        )

    def resolve(self, uri: str) -> Path:
        """Resolve only a store-generated URI to a regular, non-linked artifact."""
        relative_uri = self._parse_uri(uri)
        root = self._validated_root()
        candidate = root / relative_uri
        self._ensure_no_reparse_points(candidate)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise ArtifactNotFound("artifact does not exist") from error
        self._assert_inside(resolved, root)
        try:
            mode = resolved.stat().st_mode
        except OSError as error:
            raise ArtifactNotFound("artifact does not exist") from error
        if not stat.S_ISREG(mode):
            raise UnsafeArtifactPath("artifact URI does not name a regular file")
        return resolved

    def _validated_root(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        self._ensure_no_reparse_points(self._root)
        return self._root.resolve(strict=True)

    def _validated_workspace(self, workspace: Path) -> Path:
        if any(part == ".." for part in workspace.parts):
            raise UnsafeArtifactPath("attempt workspace must not contain parent traversal")
        try:
            self._ensure_no_reparse_points(workspace)
            resolved = workspace.resolve(strict=True)
        except OSError as error:
            raise UnsafeArtifactPath("attempt workspace is unavailable") from error
        if not resolved.is_dir():
            raise UnsafeArtifactPath("attempt workspace must be a directory")
        return resolved

    def _validated_source(self, source_path: Path, workspace: Path) -> Path:
        if any(part == ".." for part in source_path.parts):
            raise UnsafeArtifactPath("artifact source must not contain parent traversal")
        lexical_source = source_path if source_path.is_absolute() else workspace / source_path
        try:
            self._ensure_no_reparse_points(lexical_source)
            source = lexical_source.resolve(strict=True)
        except OSError as error:
            raise UnsafeArtifactPath("artifact source is unavailable") from error
        self._assert_inside(source, workspace)
        try:
            mode = source.stat().st_mode
        except OSError as error:
            raise UnsafeArtifactPath("artifact source is unavailable") from error
        if not stat.S_ISREG(mode):
            raise UnsafeArtifactPath("artifact source must be a regular file")
        return source

    def _open_regular_source(self, source: Path):
        """Open by descriptor, then prove the opened file still matches the path."""
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as error:
            raise UnsafeArtifactPath("artifact source could not be opened safely") from error
        try:
            opened_status = os.fstat(descriptor)
            if not stat.S_ISREG(opened_status.st_mode):
                raise UnsafeArtifactPath("artifact source must be a regular file")
            self._ensure_no_reparse_points(source)
            current_status = os.stat(source)
            if not os.path.samestat(opened_status, current_status):
                raise UnsafeArtifactPath("artifact source changed during validation")
            return os.fdopen(descriptor, "rb")
        except Exception:
            os.close(descriptor)
            raise

    def _validated_destination_parent(self, destination_parent: Path, root: Path) -> None:
        self._ensure_no_reparse_points(destination_parent)
        self._assert_inside(destination_parent.resolve(), root)

    @staticmethod
    def _ensure_separate_storage_root(root: Path, workspace: Path) -> None:
        for first, second in ((root, workspace), (workspace, root)):
            try:
                first.relative_to(second)
            except ValueError:
                continue
            raise UnsafeArtifactPath(
                "artifact storage root and attempt workspace must be separate"
            )

    @staticmethod
    def _assert_inside(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as error:
            raise UnsafeArtifactPath("path escapes its required root") from error

    @staticmethod
    def _ensure_no_reparse_points(path: Path) -> None:
        """Reject symlinks on POSIX and symlinks/junctions on Windows.

        ``Path.resolve`` is used only after this component walk; containment itself is
        checked with ``relative_to`` rather than a string prefix, so sibling names such
        as ``attempt-1`` and ``attempt-10`` cannot be confused.
        """
        current = Path(path.anchor) if path.is_absolute() else Path()
        for part in path.parts:
            if part in {path.anchor, "", "."}:
                continue
            current = current / part
            try:
                status = os.lstat(current)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(status.st_mode) or _is_reparse_point(status):
                raise UnsafeArtifactPath("symbolic links and reparse points are forbidden")

    @staticmethod
    def _parse_uri(uri: str) -> Path:
        candidate = Path(uri)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            raise UnsafeArtifactPath("artifact URI must be a relative store URI")
        if len(candidate.parts) != 4 or candidate.parts[0] != "runs":
            raise UnsafeArtifactPath("artifact URI is not a store-generated URI")
        try:
            UUID(candidate.parts[1])
            UUID(candidate.parts[2])
            token = UUID(hex=candidate.parts[3])
        except ValueError as error:
            raise UnsafeArtifactPath("artifact URI has invalid identifiers") from error
        if token.hex != candidate.parts[3]:
            raise UnsafeArtifactPath("artifact URI has invalid opaque token")
        return candidate


def _is_reparse_point(status: os.stat_result) -> bool:
    """Identify Windows junctions and other reparse points without platform imports."""
    return bool(getattr(status, "st_file_attributes", 0) & 0x400)
