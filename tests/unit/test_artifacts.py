from __future__ import annotations

import os
from pathlib import Path
import stat
from types import SimpleNamespace
from uuid import uuid4

import pytest

from quality_flow.infrastructure.artifacts import (
    ArtifactMetadata,
    ArtifactTooLarge,
    FileArtifactStore,
    UnsafeArtifactPath,
)
from quality_flow.infrastructure import artifacts as artifact_module


def metadata() -> ArtifactMetadata:
    return ArtifactMetadata(
        run_id=uuid4(),
        attempt_id=uuid4(),
        artifact_type="junit",
        mime_type="application/xml",
    )


def test_store_copies_only_regular_file_inside_declared_attempt_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "results.xml"
    source.write_bytes(b"<testsuites />")
    store = FileArtifactStore(tmp_path / "artifact-root")

    stored = store.put(source, metadata(), attempt_workspace=workspace)

    assert not Path(stored.uri).is_absolute()
    assert stored.uri.startswith("runs/")
    assert stored.size_bytes == len(b"<testsuites />")
    assert stored.checksum == "836d8b7f2ac3fe355e1e312c6c9b3b291adefd65c8dd83505bbddbc2a31f2bde"
    assert stored.artifact_type == "junit"
    assert stored.mime_type == "application/xml"
    assert stored.metadata["attempt_id"] == str(stored.attempt_id)
    assert store.resolve(stored.uri).read_bytes() == b"<testsuites />"


def test_store_rejects_source_outside_workspace_and_parent_traversal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("outside")
    store = FileArtifactStore(tmp_path / "artifact-root")

    with pytest.raises(UnsafeArtifactPath):
        store.put(outside, metadata(), attempt_workspace=workspace)
    with pytest.raises(UnsafeArtifactPath):
        store.put(Path("..") / "outside.log", metadata(), attempt_workspace=workspace)


def test_store_rejects_artifact_root_that_overlaps_runner_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "result.log"
    source.write_text("result")

    with pytest.raises(UnsafeArtifactPath, match="separate"):
        FileArtifactStore(workspace / "artifacts").put(
            source, metadata(), attempt_workspace=workspace
        )


def test_store_rejects_directory_missing_source_and_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("outside")
    source_link = workspace / "outside-link.log"
    try:
        source_link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"could not create a symlink: {error}")
    store = FileArtifactStore(tmp_path / "artifact-root")

    for source in (workspace, workspace / "missing.log", source_link):
        with pytest.raises(UnsafeArtifactPath):
            store.put(source, metadata(), attempt_workspace=workspace)


def test_store_rejects_windows_reparse_point_without_link_creation_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "result.log"
    source.write_text("result")
    real_lstat = os.lstat

    def lstat_with_reparse_point(path: object) -> os.stat_result | SimpleNamespace:
        if Path(path) == source:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
        return real_lstat(path)  # type: ignore[arg-type]

    monkeypatch.setattr(artifact_module.os, "lstat", lstat_with_reparse_point)
    with pytest.raises(UnsafeArtifactPath, match="reparse"):
        FileArtifactStore(tmp_path / "artifact-root").put(
            source, metadata(), attempt_workspace=workspace
        )


def test_store_enforces_limit_without_leaving_partial_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "large.log"
    source.write_bytes(b"x" * 11)
    root = tmp_path / "artifact-root"
    store = FileArtifactStore(root, max_file_bytes=10)

    with pytest.raises(ArtifactTooLarge):
        store.put(source, metadata(), attempt_workspace=workspace)

    assert not [path for path in root.rglob("*") if path.is_file()] if root.exists() else True


def test_store_opens_the_validated_source_by_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "result.log"
    source.write_text("result")
    opened_source_paths: list[Path] = []
    real_open = artifact_module.os.open

    def recording_open(path: str | bytes | os.PathLike[str], flags: int, *args: object) -> int:
        if Path(path) == source:
            opened_source_paths.append(source)
        return real_open(path, flags, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(artifact_module.os, "open", recording_open)
    FileArtifactStore(tmp_path / "artifact-root").put(
        source, metadata(), attempt_workspace=workspace
    )

    assert opened_source_paths == [source]


def test_resolve_rejects_absolute_traversal_and_linked_artifact_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "result.log"
    source.write_text("ok")
    root = tmp_path / "artifact-root"
    store = FileArtifactStore(root)
    stored = store.put(source, metadata(), attempt_workspace=workspace)

    for uri in ("../outside", str(tmp_path / "outside"), "runs/not-a-uuid"):
        with pytest.raises(UnsafeArtifactPath):
            store.resolve(uri)

    target = store.resolve(stored.uri)
    target.unlink()
    outside = tmp_path / "outside.log"
    outside.write_text("outside")
    try:
        target.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"could not create a symlink: {error}")
    with pytest.raises(UnsafeArtifactPath):
        store.resolve(stored.uri)
