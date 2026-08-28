"""Backup/import and keep-both reference rewrite contracts for Haven."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
from types import SimpleNamespace
import uuid
import zipfile
from pathlib import Path
from unittest.mock import Mock

import frontmatter
import pytest

from backup_archive import (
    BackupArchiveError,
    build_export_archive,
    build_export_archive_file,
    extract_backup_archive_file,
    read_backup_archive,
)
from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from migrate_engine import MigrateEngine
from source_bindings import attach
from source_store import SourceStore

import migrate_engine


def _config(root: Path) -> dict:
    return {
        "buckets_dir": str(root),
        "embedding": {"enabled": False},
        "limits": {"max_migrate_bucket_bytes": 64},
    }


class _Backend:
    def vector_dim(self):
        return 2


def _engine(config: dict):
    engine = EmbeddingEngine(config)
    engine.model = "test-embedding"
    engine.enabled = False
    engine._backend = _Backend()
    return engine


def _write_bucket(
    root: Path,
    bucket_id: str = "memory-1",
    content: str = "important memory",
    metadata: dict | None = None,
) -> Path:
    path = root / "dynamic" / "general" / f"memory_{bucket_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        "id": bucket_id,
        "name": "Memory",
        "type": "dynamic",
        "domain": ["general"],
        "created": "2026-07-11T12:00:00",
        **(metadata or {}),
    }
    post = frontmatter.Post(content, **values)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


def _rewrite_zip(payload: bytes, updates: dict[str, bytes]) -> bytes:
    with zipfile.ZipFile(io.BytesIO(payload), "r") as source:
        members = [(info, source.read(info.filename)) for info in source.infolist()]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info, data in members:
            target.writestr(info.filename, updates.get(info.filename, data))
    return output.getvalue()


def _manifest_for(files: dict[str, bytes]) -> bytes:
    entries = [
        {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for path, data in sorted(files.items())
    ]
    manifest = {
        "schema_version": 1,
        "kind": "ombre-brain-backup",
        "created_at": "now",
        "version": "test",
        "file_count": len(entries),
        "total_bytes": sum(entry["size"] for entry in entries),
        "files": entries,
    }
    return json.dumps(manifest).encode("utf-8")


def _engine_fixture(root: Path):
    engine = _engine(_config(root))
    config = _config(root)
    manager = BucketManager(config)
    outbox = SimpleNamespace(
        enqueue=lambda bucket_id, content: True,
        enqueue_meaning=lambda bucket_id, content: True,
        discard=lambda bucket_id: True,
        complete_content=lambda bucket_id, content: None,
        complete_meaning=lambda bucket_id, content: None,
        running=False,
    )
    manager.attach_embedding_outbox(outbox)
    manager.attach_embedding_engine(engine)
    return manager, engine, MigrateEngine(_config(root), manager, engine)


def test_export_archive_is_manifest_verified_and_includes_sources(tmp_path):
    vault = tmp_path / "source"
    bucket = _write_bucket(vault)
    source_text = "第一行\n第二行\n"
    ref = SourceStore(vault).put(source_text)

    post = frontmatter.load(bucket)
    post["source_refs"] = [{"ref": ref, "ranges": [[2, 2]]}]
    bucket.write_text(frontmatter.dumps(post), encoding="utf-8")

    payload, manifest = build_export_archive(
        str(vault), "", {"exported_at": "now", "version": "test"}
    )
    package = read_backup_archive(payload)

    assert package["integrity_verified"] is True
    assert package["integrity_warning"] == ""
    assert package["manifest"] == manifest
    assert package["files"][f"buckets/{bucket.relative_to(vault).as_posix()}"] == bucket.read_bytes()
    assert package["files"][f"sources/{ref}.source"] == source_text.encode("utf-8")
    assert manifest["file_count"] == 3


def test_new_export_rejects_dangling_source_reference(tmp_path):
    vault = tmp_path / "source"
    bucket = _write_bucket(vault, metadata={"source_refs": [{"ref": "src_" + "a" * 64, "ranges": [[1, 1]]}]})
    assert bucket.exists()

    with pytest.raises(BackupArchiveError, match="悬空原文证据引用"):
        build_export_archive(str(vault), "", {"version": "test"})
    with pytest.raises(BackupArchiveError, match="悬空原文证据引用"):
        build_export_archive_file(str(vault), "", {"version": "test"})


def test_extractor_rejects_traversal_symlink_and_unsupported_member(tmp_path):
    malicious = io.BytesIO()
    with zipfile.ZipFile(malicious, "w") as archive:
        archive.writestr("buckets/../../outside.md", b"bad")
    with pytest.raises(BackupArchiveError, match="不安全路径"):
        read_backup_archive(malicious.getvalue())

    symlink = io.BytesIO()
    with zipfile.ZipFile(symlink, "w") as archive:
        info = zipfile.ZipInfo("buckets/link.md")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"../../outside.md")
    with pytest.raises(BackupArchiveError, match="符号链接"):
        read_backup_archive(symlink.getvalue())

    junk = tmp_path / "junk.zip"
    with zipfile.ZipFile(junk, "w") as archive:
        archive.writestr("buckets/valid.md", b"---\nid: valid\n---\nbody\n")
        archive.writestr("junk.bin", b"0" * (2 * 1024 * 1024))
    with pytest.raises(BackupArchiveError, match="不支持的成员"):
        extract_backup_archive_file(str(junk), str(tmp_path / "extracted"))
    assert not any((tmp_path / "extracted").iterdir())


def test_extractor_enforces_member_and_manifest_security(tmp_path):
    archive_path = tmp_path / "large.zip"
    member_size = 10 * 1024 * 1024 + 1
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("buckets/dynamic/too-large.md", b"x" * member_size)
    with pytest.raises(BackupArchiveError, match="成员过大"):
        extract_backup_archive_file(str(archive_path), str(tmp_path / "extracted"))

    payload = build_export_archive(str(tmp_path / "missing"), "", {})[0]
    assert payload


async def test_restore_round_trip_preserves_bucket_and_source(tmp_path):
    source_vault = tmp_path / "source"
    manager, _engine_obj, export_migrate = _engine_fixture(source_vault)
    bucket_id = await manager.create("整理后事件", extra_metadata={"title": "核对标题"})
    raw = "第一行\n第二行\n第三行\n"
    source_store = SourceStore(source_vault)
    await attach(manager, source_store, bucket_id, "核对标题", raw, [[2, 2]])
    restored_source = await manager.get(bucket_id)
    ref = restored_source["metadata"]["source_links"][0]["ref"]

    payload, _ = build_export_archive(str(source_vault), "", {})
    target_vault = tmp_path / "target"
    target_manager, _target_engine, migrate = _engine_fixture(target_vault)
    parsed = await migrate.parse_zip(payload)
    assert parsed["ok"] is True
    await migrate.apply({})

    restored = await target_manager.get(bucket_id)
    assert restored["content"] == "整理后事件"
    assert restored["metadata"]["source_links"][0]["ref"] == ref
    assert SourceStore(target_vault).read(ref) == raw


async def test_import_rejects_tampered_source_member(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault)
    real_ref = SourceStore(source_vault).put("原始证据")
    payload, _ = build_export_archive(str(source_vault), "", {})
    member = f"sources/{real_ref}.source"
    changed = "攻击者替换的证据".encode("utf-8")
    with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
        manifest = json.loads(archive.read("backup_manifest.json"))
    for entry in manifest["files"]:
        if entry["path"] == member:
            manifest["total_bytes"] += len(changed) - entry["size"]
            entry["size"] = len(changed)
            entry["sha256"] = hashlib.sha256(changed).hexdigest()
            break
    tampered = _rewrite_zip(payload, {member: changed, "backup_manifest.json": json.dumps(manifest).encode()})

    manager, _engine_obj, migrate = _engine_fixture(tmp_path / "target")
    parsed = await migrate.parse_zip(tampered)

    assert parsed["ok"] is False
    assert "SHA-256" in parsed["error"]


async def test_keep_both_maps_imported_vector_to_new_id(tmp_path):
    source_vault = tmp_path / "source"
    manager, engine, export_migrate = _engine_fixture(source_vault)
    _bucket_id = await manager.create("imported version", bucket_id="memory-1")
    sqlite3.connect(engine.db_path).close()
    connection = sqlite3.connect(engine.db_path)
    connection.execute(
        "INSERT INTO embeddings (bucket_id, embedding, model, dimension, updated_at, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
        (_bucket_id, "[1,2]", "test-embedding", 2, "2026-08-28T00:00:00", "hash"),
    )
    connection.commit()
    connection.close()
    payload, _ = build_export_archive(str(source_vault), engine.db_path, {"embedding": {"model": "test-embedding", "dim": 2}})

    target_vault = tmp_path / "target"
    _write_bucket(target_vault, content="local version")
    target_manager, target_engine, migrate = _engine_fixture(target_vault)
    parsed = await migrate.parse_zip(payload)
    assert parsed["conflicts_count"] == 1
    await migrate.apply({parsed["conflicts"][0]["bucket_id"]: "keep_both"})

    buckets = await target_manager.list_all()
    assert {bucket["content"] for bucket in buckets} == {"local version", "imported version"}
    imported = next(bucket for bucket in buckets if bucket["content"] == "imported version")
    assert imported["id"] != _bucket_id
    with sqlite3.connect(target_engine.db_path) as connection:
        row = connection.execute(
            "SELECT embedding FROM embeddings WHERE bucket_id = ?", (imported["id"],)
        ).fetchone()
    assert row and json.loads(row[0]) == [1, 2]


def test_keep_both_relation_target_remap_preserves_other_ledger_fields(tmp_path):
    path = _write_bucket(tmp_path, bucket_id="source", metadata={"relation_links": [
        {"target_bucket_id": "old-target", "type": "causes", "label": "", "status": "detached"},
    ]})
    migrate = object.__new__(MigrateEngine)
    migrate._atomic_write = lambda target, content: Path(target).write_text(content, encoding="utf-8")

    migrate._remap_imported_relation_targets(
        {"source": str(path)},
        {"old-target": "new-target"},
        frozenset({"source", "old-target"}),
    )

    remapped = frontmatter.load(path).metadata["relation_links"][0]
    assert remapped == {"target_bucket_id": "new-target", "type": "causes", "label": "", "status": "detached"}


async def test_overwrite_preserves_old_memory_under_unique_archived_id(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="imported version")
    payload, _ = build_export_archive(str(source_vault), "", {"embedding": {"model": "test-embedding", "dim": 2}})

    target_vault = tmp_path / "target"
    _write_bucket(target_vault, content="local version")
    manager, _engine_obj, migrate = _engine_fixture(target_vault)
    await migrate.parse_zip(payload)
    await migrate.apply({"memory-1": "overwrite"})

    buckets = await manager.list_all(include_archive=True)
    assert {bucket["content"] for bucket in buckets} == {"local version", "imported version"}
    archived = next(bucket for bucket in buckets if bucket["content"] == "local version")
    assert archived["id"].startswith("memory-1-superseded-")
    assert archived["metadata"]["superseded_by"] == "memory-1"


async def test_overwrite_leaves_old_memory_untouched_when_new_content_write_fails(tmp_path, monkeypatch):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="imported version")
    payload, _ = build_export_archive(str(source_vault), "", {"embedding": {"model": "test-embedding", "dim": 2}})

    target_vault = tmp_path / "target"
    _write_bucket(target_vault, content="local version")
    manager, _engine_obj, migrate = _engine_fixture(target_vault)
    def _boom(self, pb, target_id, buckets_dir):
        raise OSError("simulated disk failure while staging new content")
    monkeypatch.setattr(MigrateEngine, "_write_bucket_file_staged", _boom)

    await migrate.parse_zip(payload)
    await migrate.apply({"memory-1": "overwrite"})

    buckets = await manager.list_all(include_archive=True)
    assert len(buckets) == 1
    assert buckets[0]["id"] == "memory-1"
    assert buckets[0]["content"] == "local version"
    assert migrate._apply_errors


async def test_overwrite_cleans_up_staged_file_when_old_bucket_handling_fails(tmp_path, monkeypatch):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="imported version")
    payload, _ = build_export_archive(str(source_vault), "", {"embedding": {"model": "test-embedding", "dim": 2}})

    target_vault = tmp_path / "target"
    _write_bucket(target_vault, content="local version")
    manager, _engine_obj, migrate = _engine_fixture(target_vault)
    def _boom(self, existing_path, bucket_id, buckets_dir):
        raise OSError("simulated failure while archiving the old bucket")
    monkeypatch.setattr(MigrateEngine, "_write_historical_copy", _boom)

    await migrate.parse_zip(payload)
    await migrate.apply({"memory-1": "overwrite"})

    staged_leftovers = list((target_vault / "dynamic").rglob("*.staging-*"))
    assert staged_leftovers == []
    assert migrate._apply_errors
    buckets = await manager.list_all(include_archive=True)
    assert [(bucket["id"], bucket["content"]) for bucket in buckets] == [("memory-1", "local version")]


async def test_missing_snapshot_vector_is_durably_queued(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault)
    payload, _ = build_export_archive(str(source_vault), "", {"embedding": {"model": "test-embedding", "dim": 2}})

    target_vault = tmp_path / "target"
    manager, _engine_obj, migrate = _engine_fixture(target_vault)
    manager.embedding_outbox = SimpleNamespace(enqueue=Mock(return_value=True))

    assert (await migrate.parse_zip(payload))["ok"] is True
    await migrate.apply({})
    assert manager.embedding_outbox.enqueue.call_args[0] == ("memory-1", "important memory")


async def test_apply_rechecks_conflict_created_after_parse(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="imported version")
    payload, _ = build_export_archive(str(source_vault), "", {"embedding": {"model": "test-embedding", "dim": 2}})

    target_vault = tmp_path / "target"
    manager, _engine_obj, migrate = _engine_fixture(target_vault)
    parsed = await migrate.parse_zip(payload)
    assert parsed["conflicts_count"] == 0
    _write_bucket(target_vault, content="created after parse")

    await migrate.apply({})

    buckets = await manager.list_all(include_archive=True)
    assert [(bucket["id"], bucket["content"]) for bucket in buckets] == [
        ("memory-1", "created after parse")
    ]
    assert migrate.get_status()["result"] == {"imported": 0, "skipped": 1}
    assert "新冲突" in " ".join(migrate._apply_errors)


async def test_migrate_rejects_bucket_over_runtime_content_limit(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault, content="x" * 65)
    payload, _ = build_export_archive(str(source_vault), "", {"embedding": {}})

    manager, _engine_obj, migrate = _engine_fixture(tmp_path / "target")
    result = await migrate.parse_zip(payload)

    assert result["ok"] is False
    assert "正文过大" in result["error"]


async def test_migrate_rejects_non_json_safe_yaml_metadata(tmp_path):
    source_vault = tmp_path / "source"
    path = source_vault / "dynamic" / "general" / "unsafe.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: unsafe-metadata\npayload: !!set\n  ? value\n---\nbody\n", encoding="utf-8")
    payload, _ = build_export_archive(str(source_vault), "", {"embedding": {}})

    manager, _engine_obj, migrate = _engine_fixture(tmp_path / "target")
    result = await migrate.parse_zip(payload)

    assert result["ok"] is False
    assert "JSON-safe" in result["error"]


async def test_overwrite_rolls_back_when_old_source_cannot_be_removed(tmp_path, monkeypatch):
    source_vault = tmp_path / "source"
    source_path = _write_bucket(source_vault, content="imported permanent")
    source_post = frontmatter.load(source_path)
    source_post["type"] = "permanent"
    source_path.write_text(frontmatter.dumps(source_post), encoding="utf-8")
    payload, _ = build_export_archive(str(source_vault), "", {"embedding": {"model": "test-embedding", "dim": 2}})

    target_vault = tmp_path / "target"
    old_path = _write_bucket(target_vault, content="local survivor")
    manager, _engine_obj, migrate = _engine_fixture(target_vault)
    await migrate.parse_zip(payload)

    original_unlink = os.unlink
    expected = os.path.normcase(os.path.abspath(str(old_path)))
    def fail_old_source(path, *args, **kwargs):
        normalized = str(path)
        if normalized.startswith("\\\\?\\"):
            normalized = normalized[4:]
        if os.path.normcase(os.path.abspath(normalized)) == expected:
            raise OSError("simulated old source unlink failure")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(migrate_engine.os, "unlink", fail_old_source)

    await migrate.apply({"memory-1": "overwrite"})

    buckets = await manager.list_all(include_archive=True)
    assert [(bucket["id"], bucket["content"]) for bucket in buckets] == [("memory-1", "local survivor")]
    assert migrate._apply_errors
    assert not list(target_vault.rglob("*.staging-*"))


async def test_disk_backed_parse_releases_extracted_payload_after_apply(tmp_path):
    source_vault = tmp_path / "source"
    _write_bucket(source_vault)
    archive_path, _manifest = build_export_archive_file(
        str(source_vault), "", {"embedding": {"model": "test-embedding", "dim": 2}}
    )

    target_vault = tmp_path / "target"
    manager, _engine_obj, migrate = _engine_fixture(target_vault)
    reservation = migrate.reserve_parse()
    try:
        parsed = await migrate.parse_zip_file(archive_path, reservation_id=reservation)
    finally:
        os.unlink(archive_path)

    assert parsed["ok"] is True
    assert migrate._parsed_buckets[0].md_bytes is None
    assert os.path.isfile(migrate._parsed_buckets[0].md_path)
    await migrate.apply({})
    assert not os.path.exists(migrate._parse_temp_dir)
    assert (await manager.get("memory-1"))["content"] == "important memory"
