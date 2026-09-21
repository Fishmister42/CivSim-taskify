"""Run bundles: a ``RunRecordSet`` on disk, and back (003 US4; contract V5, V6; FR-027, FR-028).

A bundle is written and read **over the contract**, never over a store's file layout: the
exporter takes what ``export_run`` returned, the importer hands ``import_run`` what it read. So a
bundle produced on one host by one store implementation imports into any other.

**Layout** (data-model.md SS3.7). The directory is canonical; the ``.tar.gz`` is exactly that
directory, archived, and is what crosses hosts as one file (research R7)::

    <run_id>.civsim-bundle/
        manifest.json         counts, per-file SHA-256, image hashes, source host and store id
        run.json              Run
        configuration.json    RunConfiguration (wire shape, by alias)
        turn_cycles.jsonl     one TurnCycleRecord per line, every attempt, by turn then attempt
        run_events.jsonl
        model_calls.jsonl
        save_points.jsonl
        captures.jsonl
        images/<sha256>       kept captures' bytes, content-addressed

Every path in the manifest is a relative POSIX path (forward slashes, no drive, no ``..``), so
the same bundle reads identically on Linux and Windows (FR-028). Every file is hashed and the
importer verifies every hash before a single record is handed to the store (V6).
"""

from __future__ import annotations

import hashlib
import io
import platform
import tarfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from civsim_harness.errors import BundleError
from civsim_harness.models.config import RunConfiguration
from civsim_harness.models.records import ModelCall, RunEvent, SavePoint
from civsim_harness.models.run import Run
from civsim_harness.models.turn import ScreenCapture, ScreeningStatus
from civsim_harness.store.contract import (
    BUNDLE_FORMAT,
    BundleManifest,
    RunRecordSet,
    StoreSchemaVersion,
)
from civsim_harness.store.port import TurnCycleRecord
from civsim_harness.store.schema import STORE_SCHEMA_VERSION

BUNDLE_SUFFIX = ".civsim-bundle"
ARCHIVE_SUFFIX = ".civsim-bundle.tar.gz"
MANIFEST_NAME = "manifest.json"
IMAGES_DIR = "images"

_RECORD_FILES: tuple[str, ...] = (
    "run.json",
    "configuration.json",
    "turn_cycles.jsonl",
    "run_events.jsonl",
    "model_calls.jsonl",
    "save_points.jsonl",
    "captures.jsonl",
)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _jsonl(records: Iterable[Any]) -> bytes:
    lines = [record.model_dump_json() for record in records]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def _parse_jsonl(model: Any, data: bytes) -> tuple[Any, ...]:
    text = data.decode("utf-8")
    return tuple(model.model_validate_json(line) for line in text.splitlines() if line.strip())


def bundle_dir_name(run_id: str) -> str:
    return f"{run_id}{BUNDLE_SUFFIX}"


# --------------------------------------------------------------------------
# Serialisation to an in-memory file map
# --------------------------------------------------------------------------


def render_bundle(
    records: RunRecordSet,
    *,
    store_schema_version: str,
    source_store_id: str,
    source_host: str | None = None,
    now: datetime | None = None,
) -> dict[str, bytes]:
    """``{relative POSIX path: bytes}`` for every file of the bundle, manifest included."""
    files: dict[str, bytes] = {
        "run.json": records.run.model_dump_json().encode("utf-8"),
        "configuration.json": records.configuration.model_dump_json(by_alias=True).encode("utf-8"),
        "turn_cycles.jsonl": _jsonl(records.turn_cycles),
        "run_events.jsonl": _jsonl(records.run_events),
        "model_calls.jsonl": _jsonl(records.model_calls),
        "save_points.jsonl": _jsonl(records.save_points),
        "captures.jsonl": _jsonl(records.captures),
    }
    image_hashes: dict[str, str] = {}
    for capture in records.captures:
        if capture.screening_status is not ScreeningStatus.SCREENED_CLEAN or not capture.blob_ref:
            continue
        content = records.images.get(capture.blob_ref)
        if content is None:
            raise BundleError(
                "cannot export: a kept capture's image is not in the record set",
                detail={"capture_id": capture.capture_id, "blob_ref": capture.blob_ref},
            )
        digest = sha256_of(content)
        if digest != capture.blob_ref:
            raise BundleError(
                "cannot export: image bytes do not hash to the capture's blob_ref",
                detail={"capture_id": capture.capture_id, "blob_ref": capture.blob_ref},
            )
        files[f"{IMAGES_DIR}/{digest}"] = content
        image_hashes[capture.capture_id] = digest

    manifest = BundleManifest(
        bundle_format=BUNDLE_FORMAT,
        store_schema_version=store_schema_version,
        run_id=records.run.run_id,
        exported_at=now or datetime.now(UTC),
        source_host=source_host or platform.system() or "unknown",
        source_store_id=source_store_id,
        counts=records.counts(),
        files={path: sha256_of(content) for path, content in files.items()},
        images=image_hashes,
    )
    files[MANIFEST_NAME] = manifest.model_dump_json(indent=2).encode("utf-8")
    return files


def parse_bundle(files: dict[str, bytes]) -> tuple[BundleManifest, RunRecordSet]:
    """Validate a file map against its manifest and rebuild the record set (V6)."""
    raw_manifest = files.get(MANIFEST_NAME)
    if raw_manifest is None:
        raise BundleError("bundle has no manifest.json")
    try:
        manifest = BundleManifest.model_validate_json(raw_manifest)
    except ValueError as exc:
        raise BundleError("bundle manifest is malformed", detail={"error": str(exc)}) from exc

    if manifest.bundle_format > BUNDLE_FORMAT:
        raise BundleError(
            "bundle was written in a newer bundle format than this importer understands",
            detail={"bundle_format": manifest.bundle_format, "supported": BUNDLE_FORMAT},
        )
    try:
        written_by = StoreSchemaVersion.parse(manifest.store_schema_version)
    except ValueError as exc:
        raise BundleError(
            "bundle names an unreadable store schema version",
            detail={"store_schema_version": manifest.store_schema_version},
        ) from exc
    if not written_by.readable_by(STORE_SCHEMA_VERSION):
        raise BundleError(
            "bundle was written by a newer store schema major version than this importer",
            detail={
                "bundle_schema_version": str(written_by),
                "importer_schema_version": str(STORE_SCHEMA_VERSION),
            },
        )

    for path, expected in manifest.files.items():
        if "\\" in path or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise BundleError("bundle manifest names a non-portable path", detail={"path": path})
        content = files.get(path)
        if content is None:
            raise BundleError("bundle is missing a file its manifest names", detail={"path": path})
        actual = sha256_of(content)
        if actual != expected:
            raise BundleError(
                "bundle file does not match its manifest hash",
                detail={"path": path, "expected": expected, "actual": actual},
            )
    for name in _RECORD_FILES:
        if name not in manifest.files:
            raise BundleError(
                "bundle manifest does not name a required file", detail={"path": name}
            )

    run = Run.model_validate_json(files["run.json"])
    if run.run_id != manifest.run_id:
        raise BundleError(
            "bundle manifest and run.json disagree on the run id",
            detail={"manifest": manifest.run_id, "run_json": run.run_id},
        )
    configuration = RunConfiguration.model_validate_json(files["configuration.json"])
    turn_cycles = _parse_jsonl(TurnCycleRecord, files["turn_cycles.jsonl"])
    run_events = _parse_jsonl(RunEvent, files["run_events.jsonl"])
    model_calls = _parse_jsonl(ModelCall, files["model_calls.jsonl"])
    save_points = _parse_jsonl(SavePoint, files["save_points.jsonl"])
    captures = _parse_jsonl(ScreenCapture, files["captures.jsonl"])

    images: dict[str, bytes] = {}
    for capture in captures:
        if capture.screening_status is not ScreeningStatus.SCREENED_CLEAN or not capture.blob_ref:
            continue
        content = files.get(f"{IMAGES_DIR}/{capture.blob_ref}")
        if content is None:
            raise BundleError(
                "bundle is missing the image of a kept capture",
                detail={"capture_id": capture.capture_id, "blob_ref": capture.blob_ref},
            )
        images[capture.blob_ref] = content

    records = RunRecordSet(
        run=run,
        configuration=configuration,
        turn_cycles=turn_cycles,
        run_events=run_events,
        model_calls=model_calls,
        save_points=save_points,
        captures=captures,
        images=images,
    )
    if records.counts() != manifest.counts:
        raise BundleError(
            "bundle contents do not match the manifest's counts",
            detail={"manifest": manifest.counts, "actual": records.counts()},
        )
    return manifest, records


# --------------------------------------------------------------------------
# Disk: directory and archive
# --------------------------------------------------------------------------


def write_bundle(
    records: RunRecordSet,
    destination_dir: Path,
    *,
    store_schema_version: str,
    source_store_id: str,
    archive: bool = False,
    source_host: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Write ``<destination_dir>/<run_id>.civsim-bundle/`` and, with *archive*, the ``.tar.gz``
    of exactly that directory beside it. Returns the directory (or the archive when requested).
    Refuses to overwrite an existing bundle directory or archive."""
    files = render_bundle(
        records,
        store_schema_version=store_schema_version,
        source_store_id=source_store_id,
        source_host=source_host,
        now=now,
    )
    bundle_dir = destination_dir / bundle_dir_name(records.run.run_id)
    if bundle_dir.exists():
        raise BundleError("bundle directory already exists", detail={"path": str(bundle_dir)})
    bundle_dir.mkdir(parents=True)
    for relative, content in files.items():
        target = bundle_dir / PurePosixPath(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    if not archive:
        return bundle_dir

    archive_path = destination_dir / f"{records.run.run_id}{ARCHIVE_SUFFIX}"
    if archive_path.exists():
        raise BundleError("bundle archive already exists", detail={"path": str(archive_path)})
    with tarfile.open(archive_path, "w:gz") as tar:
        for relative in sorted(files):
            info = tarfile.TarInfo(name=f"{bundle_dir.name}/{relative}")
            content = files[relative]
            info.size = len(content)
            info.mtime = int((now or datetime.now(UTC)).timestamp())
            tar.addfile(info, io.BytesIO(content))
    return archive_path


def _read_directory(bundle_dir: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in bundle_dir.rglob("*"):
        if path.is_file():
            files[path.relative_to(bundle_dir).as_posix()] = path.read_bytes()
    return files


def _read_archive(archive_path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(archive_path, "r:gz") as tar:
        members = [member for member in tar.getmembers() if member.isfile()]
        if not members:
            raise BundleError("bundle archive is empty", detail={"path": str(archive_path)})
        roots = {PurePosixPath(member.name).parts[0] for member in members}
        if len(roots) != 1:
            raise BundleError(
                "bundle archive must contain exactly one bundle directory",
                detail={"roots": sorted(roots)},
            )
        for member in members:
            parts = PurePosixPath(member.name).parts[1:]
            if not parts or ".." in parts:
                raise BundleError(
                    "bundle archive names a non-portable path", detail={"path": member.name}
                )
            handle = tar.extractfile(member)
            if handle is None:  # pragma: no cover - isfile() filtered above
                continue
            files[PurePosixPath(*parts).as_posix()] = handle.read()
    return files


def read_bundle(path: Path) -> tuple[BundleManifest, RunRecordSet]:
    """Read a bundle directory or ``.tar.gz`` archive, verifying every hash (V6)."""
    if path.is_dir():
        files = _read_directory(path)
    elif path.is_file():
        files = _read_archive(path)
    else:
        raise BundleError("bundle path does not exist", detail={"path": str(path)})
    return parse_bundle(files)


__all__ = [
    "ARCHIVE_SUFFIX",
    "BUNDLE_SUFFIX",
    "IMAGES_DIR",
    "MANIFEST_NAME",
    "bundle_dir_name",
    "parse_bundle",
    "read_bundle",
    "render_bundle",
    "sha256_of",
    "write_bundle",
]
