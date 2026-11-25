"""Portable, integrity-checked AnimWeaver scene packages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


MATERIAL_ID = re.compile(r"[0-9a-f]{24}")
FORMAT = "animweaver.scene"
VERSION = 1
MAX_FILES = 20_000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024


def _references(value):
    if isinstance(value, str):
        if MATERIAL_ID.fullmatch(value):
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def _closure(work, scene: dict):
    pending = [scene["id"]]
    records = {}
    while pending:
        ref_id = pending.pop()
        if ref_id in records:
            continue
        record = work.load(ref_id)
        records[ref_id] = record
        for dependency in _references(record):
            if dependency in records or dependency == ref_id:
                continue
            try:
                work.load(dependency)
            except (FileNotFoundError, ValueError) as exc:
                raise ValueError(
                    f"Scene dependency {dependency} is missing from the material store"
                ) from exc
            pending.append(dependency)
    return records


def _material_files(work, records: dict):
    entries = {}
    for ref_id, record in sorted(records.items()):
        directory = work.path(f".reference-materials/{ref_id}")
        record_file = directory / "record.json"
        if not record_file.is_file():
            raise ValueError(f"Material {ref_id} has no record")
        entries[f"materials/{ref_id}/record.json"] = record_file
        for logical, relative in sorted(record.get("files", {}).items()):
            file = work.path(relative)
            if file.parent != directory or file.name != logical or not file.is_file():
                raise ValueError(f"Material {ref_id} has an invalid file mapping")
            entries[f"materials/{ref_id}/{logical}"] = file
    return entries


def _file_hash(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _zip_write(archive, name: str, data: bytes):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def export(work, scene: dict, output: str) -> dict:
    if scene["kind"] != "cutout_scene":
        raise ValueError("Choose an editable cutout scene")
    target = work.path(output)
    if target.suffix.lower() != ".animweaver" or target.is_relative_to(work.store):
        raise ValueError("Choose a workspace .animweaver path outside the material store")
    if not target.parent.is_dir():
        raise ValueError("The package output directory must exist")
    if target.exists():
        raise ValueError("Package output must be a new file")
    records = _closure(work, scene)
    files = _material_files(work, records)
    entries = {
        name: {"bytes": path.stat().st_size, "sha256": _file_hash(path)}
        for name, path in sorted(files.items())
    }
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "root_scene": scene["id"],
        "materials": sorted(records),
        "entries": entries,
    }
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=".animweaver-", suffix=".tmp", delete=False
    ) as stream:
        staged = Path(stream.name)
    try:
        with zipfile.ZipFile(staged, "w") as archive:
            _zip_write(
                archive,
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            )
            for name, path in sorted(files.items()):
                _zip_write(archive, name, path.read_bytes())
        with staged.open("rb") as source, target.open("xb") as destination:
            try:
                shutil.copyfileobj(source, destination)
            except BaseException:
                destination.close()
                target.unlink(missing_ok=True)
                raise
    finally:
        staged.unlink(missing_ok=True)
    return work.create(
        dict(
            kind="scene_package_export",
            origin="derived",
            parent=scene["id"],
            scene=scene["id"],
            path=work.rel(target),
            sha256=work.digest(target),
            materials=len(records),
            entries=len(entries),
            bytes=target.stat().st_size,
        )
    )


def _safe_members(archive: zipfile.ZipFile):
    infos = archive.infolist()
    if len(infos) > MAX_FILES:
        raise ValueError("AnimWeaver package contains too many files")
    total = 0
    names = set()
    for info in infos:
        path = PurePosixPath(info.filename)
        if (
            info.is_dir()
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in info.filename
            or not path.parts
            or info.filename in names
        ):
            raise ValueError("AnimWeaver package contains an unsafe archive path")
        if info.file_size > MAX_FILE_BYTES:
            raise ValueError("AnimWeaver package contains an oversized file")
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("AnimWeaver package is too large to import")
        names.add(info.filename)
    return {info.filename: info for info in infos}


def _archive_hash(archive, name: str):
    digest = hashlib.sha256()
    with archive.open(name) as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _validate_manifest(archive, members):
    try:
        manifest_info = members["manifest.json"]
    except KeyError as exc:
        raise ValueError("AnimWeaver package has no manifest") from exc
    if manifest_info.file_size > 4 * 1024 * 1024:
        raise ValueError("AnimWeaver package manifest is too large")
    try:
        manifest = json.loads(archive.read("manifest.json"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("AnimWeaver package manifest is invalid JSON") from exc
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise ValueError("Unsupported AnimWeaver package format or version")
    root = manifest.get("root_scene")
    materials = manifest.get("materials")
    entries = manifest.get("entries")
    if (
        not isinstance(root, str)
        or not MATERIAL_ID.fullmatch(root)
        or not isinstance(materials, list)
        or not materials
        or len(set(materials)) != len(materials)
        or any(not isinstance(item, str) or not MATERIAL_ID.fullmatch(item) for item in materials)
        or root not in materials
        or not isinstance(entries, dict)
    ):
        raise ValueError("AnimWeaver package manifest has invalid material IDs")
    if set(members) != {"manifest.json", *entries}:
        raise ValueError("AnimWeaver package entries do not match its manifest")
    material_set = set(materials)
    for name, expected in entries.items():
        path = PurePosixPath(name)
        if (
            len(path.parts) != 3
            or path.parts[0] != "materials"
            or path.parts[1] not in material_set
            or not isinstance(expected, dict)
            or expected.get("bytes") != members[name].file_size
            or not isinstance(expected.get("sha256"), str)
        ):
            raise ValueError("AnimWeaver package has an invalid material entry")
        if _archive_hash(archive, name) != expected["sha256"]:
            raise ValueError(f"AnimWeaver package checksum failed for {name}")
    return manifest


def _records(archive, manifest):
    records = {}
    for ref_id in manifest["materials"]:
        name = f"materials/{ref_id}/record.json"
        if name not in manifest["entries"]:
            raise ValueError(f"AnimWeaver package material {ref_id} has no record")
        if manifest["entries"][name]["bytes"] > 4 * 1024 * 1024:
            raise ValueError(f"AnimWeaver material {ref_id} record is too large")
        try:
            record = json.loads(archive.read(name))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"AnimWeaver material {ref_id} has invalid JSON") from exc
        if record.get("id") != ref_id or not isinstance(record.get("files"), dict):
            raise ValueError(f"AnimWeaver material {ref_id} has an invalid record")
        expected_files = {name}
        for logical, relative in record["files"].items():
            if (
                not isinstance(logical, str)
                or PurePosixPath(logical).name != logical
                or relative != f".reference-materials/{ref_id}/{logical}"
            ):
                raise ValueError(f"AnimWeaver material {ref_id} has an unsafe file mapping")
            expected_files.add(f"materials/{ref_id}/{logical}")
        actual_files = {
            entry
            for entry in manifest["entries"]
            if entry.startswith(f"materials/{ref_id}/")
        }
        if actual_files != expected_files:
            raise ValueError(f"AnimWeaver material {ref_id} files do not match its record")
        records[ref_id] = record
    root = records[manifest["root_scene"]]
    if root.get("kind") != "cutout_scene":
        raise ValueError("AnimWeaver package root is not an editable scene")
    included = set(records)
    unresolved = sorted(
        {
            dependency
            for record in records.values()
            for dependency in _references(record)
            if dependency not in included
        }
    )
    if unresolved:
        raise ValueError(
            f"AnimWeaver package is missing material dependencies: {', '.join(unresolved)}"
        )
    return records


def _existing_matches(work, archive, ref_id: str, record: dict):
    directory = work.path(f".reference-materials/{ref_id}")
    if not directory.exists():
        return False
    expected = {"record.json", *record["files"]}
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != expected:
        raise ValueError(f"Material ID conflict while importing {ref_id}")
    for name in expected:
        local = directory / name
        if _file_hash(local) != _archive_hash(archive, f"materials/{ref_id}/{name}"):
            raise ValueError(f"Material ID conflict while importing {ref_id}")
    return True


def import_package(work, name: str) -> dict:
    path = work.path(name)
    if (
        not path.is_file()
        or path.suffix.lower() != ".animweaver"
        or path.is_relative_to(work.store)
    ):
        raise ValueError("Select a workspace .animweaver package outside the material store")
    checksum = work.digest(path)
    try:
        archive = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile as exc:
        raise ValueError("AnimWeaver package is not a readable ZIP container") from exc
    with archive:
        members = _safe_members(archive)
        manifest = _validate_manifest(archive, members)
        records = _records(archive, manifest)
        reused = {
            ref_id
            for ref_id, record in records.items()
            if _existing_matches(work, archive, ref_id, record)
        }
        new = [ref_id for ref_id in records if ref_id not in reused]
        with tempfile.TemporaryDirectory(dir=work.root, prefix=".animweaver-import-") as tmp:
            stage = Path(tmp)
            for ref_id in new:
                directory = stage / ref_id
                directory.mkdir()
                for filename in ("record.json", *records[ref_id]["files"]):
                    with archive.open(f"materials/{ref_id}/{filename}") as source:
                        with (directory / filename).open("xb") as destination:
                            shutil.copyfileobj(source, destination)
            for ref_id in new:
                os.replace(stage / ref_id, work.path(f".reference-materials/{ref_id}"))
    if work.digest(path) != checksum:
        raise ValueError("AnimWeaver package changed during import")
    root = work.load(manifest["root_scene"])
    receipt = work.create(
        dict(
            kind="scene_package_import",
            origin="imported",
            parent=root["id"],
            scene=root["id"],
            path=work.rel(path),
            sha256=checksum,
            imported=len(records) - len(reused),
            reused=len(reused),
            materials=len(records),
        )
    )
    return {"scene": root, "receipt": receipt}
