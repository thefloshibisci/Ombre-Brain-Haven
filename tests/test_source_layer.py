import asyncio
import hashlib
import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from bucket_manager import BucketManager
from source_bindings import attach, detach, restore
from source_read import dispatch as read
from source_store import (
    MAX_SOURCE_LINKS,
    MAX_SOURCE_REFS,
    SourceStore,
    normalize_source_ranges,
    referenced_source_ids_from_markdown,
)


def make_store(tmp_path):
    return SourceStore(tmp_path / "vault")


def make_manager(tmp_path):
    return BucketManager({"buckets_dir": str(tmp_path / "vault")})


async def make_bucket(tmp_path, title="精确标题"):
    manager = make_manager(tmp_path)
    bucket_id = await manager.create(
        content="事件结论记忆。",
        extra_metadata={"title": title},
    )
    return manager, bucket_id


def test_source_store_is_content_addressed_and_tamper_evident(tmp_path):
    store = make_store(tmp_path)
    ref = store.put("第一行\n第二行\n")
    raw = (store.root / f"{ref}.source").read_text(encoding="utf-8")

    assert ref == "src_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert store.read(ref) == raw
    assert store.put(raw) == ref
    (store.root / f"{ref}.source").write_text("tampered", encoding="utf-8")
    with pytest.raises(OSError, match="完整性校验失败"):
        store.read(ref)


def test_source_ranges_are_merged_sorted_and_strict():
    assert normalize_source_ranges([[3, 4], [1, 2], [2, 3]]) == [[1, 4]]
    for value in ([0, 1], [[2, 1]], [[True, 1]], [[1.5, 2]], [["1", 2]], ["1-2"]):
        with pytest.raises(ValueError):
            normalize_source_ranges(value)


def test_referenced_source_ids_unions_both_projections_and_rejects_bad_refs():
    metadata = {
        "source_refs": [{"ref": "src_" + "a" * 64, "ranges": [[1, 1]]}],
        "source_links": [{"ref": "src_" + "b" * 64, "ranges": [[2, 2]], "status": "active"}],
    }
    ids = referenced_source_ids_from_markdown(
        "---\nid: test\n" + "\n".join(f"{key}: {value!r}" for key, value in metadata.items()) + "\n---\nbody"
    )
    assert len(ids) == 2
    with pytest.raises(ValueError):
        referenced_source_ids_from_markdown(
            "---\nsource_refs:\n  - ref: not-a-ref\n---\n"
        )


async def test_source_attach_is_reversible_and_stable(tmp_path):
    manager, bucket_id = await make_bucket(tmp_path)
    store = make_store(tmp_path)
    content = "第一行\n第二行\n第三行\n"

    result = await attach(manager, store, bucket_id, "精确标题", content, [[2, 3]])
    assert result == f"source_attach ok bucket_id={bucket_id} slot=1 status=active ranges=2-3"

    repeated = await attach(manager, store, bucket_id, "精确标题", content, [[2, 3]])
    assert repeated == f"source_attach ok bucket_id={bucket_id} slot=1 status=active ranges=2-3"

    detached = await detach(manager, bucket_id, "精确标题", 1)
    assert detached == f"source_detach ok bucket_id={bucket_id} slot=1 status=detached"

    manifest = await read(manager, store, bucket_id, "精确标题")
    assert manifest == "source manifest\nslot=1 | ranges=2-3 | detached"

    restored = await restore(manager, bucket_id, "精确标题", 1)
    assert restored == f"source_restore ok bucket_id={bucket_id} slot=1 status=active"

    event = await read(manager, store, bucket_id, "精确标题", max_tokens=200)
    assert event.startswith(f"bucket_id={bucket_id}\ntitle=精确标题\nscope=event\n")
    assert "第二行\n第三行" in event
    assert "第一行" not in event.split("\n\n", 1)[-1]


async def test_source_binding_uses_exact_title_and_slot_contract(tmp_path):
    manager, bucket_id = await make_bucket(tmp_path)
    store = make_store(tmp_path)

    assert await attach(manager, store, bucket_id, "错误标题", "证据", [[1, 1]]) == (
        "标题不匹配，拒绝修改原文证据。"
    )
    assert await read(manager, store, bucket_id, "错误标题") == (
        "标题不匹配，拒绝读取原文。请使用该桶的精确 title。"
    )

    await attach(manager, store, bucket_id, "精确标题", "A\nB\n", [[1, 1]])
    await attach(manager, store, bucket_id, "精确标题", "C\nD\n", [[1, 1]])
    assert await detach(manager, bucket_id, "精确标题", 2) == (
        f"source_detach ok bucket_id={bucket_id} slot=2 status=detached"
    )
    assert await restore(manager, bucket_id, "精确标题", 2) == (
        f"source_restore ok bucket_id={bucket_id} slot=2 status=active"
    )
    assert await detach(manager, bucket_id, "精确标题", 3) == "source_slot=3 不存在。"


async def test_event_scope_honors_empty_ranges_declared_directly(tmp_path):
    manager, bucket_id = await make_bucket(tmp_path)
    store = make_store(tmp_path)

    await attach(manager, store, bucket_id, "精确标题", "秘密全文\n")

    def declare_empty_range(post):
        links = post.metadata.get("source_links", [])
        links[0]["ranges"] = []
        post["source_links"] = links
        post["source_refs"] = []
        return True, "declared"

    await manager.mutate_source_links(bucket_id, declare_empty_range)

    assert await read(manager, store, bucket_id, "精确标题", scope="event", max_tokens=200) == (
        "该桶未声明事件原文范围，拒绝将整份原文作为事件返回。"
        "如确需整份原文，请显式使用 scope=full_source。"
    )
    full = await read(manager, store, bucket_id, "精确标题", scope="full_source", max_tokens=200)
    assert "秘密全文" in full


async def test_invalid_ref_projection_is_rejected_before_store_read(tmp_path):
    manager, bucket_id = await make_bucket(tmp_path)
    store = make_store(tmp_path)
    ref = store.put("valid\n")
    called = False
    original_read = store.read

    def spy(_ref):
        nonlocal called
        called = True
        return original_read(_ref)

    store.read = spy

    def mutation(post):
        post["source_links"] = [{"ref": "not-a-ref", "ranges": [[1, 1]], "status": "active"}]
        post["source_refs"] = [{"ref": "not-a-ref", "ranges": [[1, 1]]}]
        return True, "injected"

    await manager.mutate_source_links(bucket_id, mutation)
    assert await read(manager, store, bucket_id, "精确标题") == (
        "该桶的原文证据引用格式无效，拒绝读取。"
    )
    assert called is False


async def test_control_characters_in_title_are_escaped(tmp_path):
    control_title = "bad" + chr(0) + "title"
    manager, bucket_id = await make_bucket(tmp_path, title=control_title)
    store = make_store(tmp_path)
    await attach(manager, store, bucket_id, control_title, "A\nB\n", [[1, 1]])
    page = await read(manager, store, bucket_id, control_title, max_tokens=400)
    assert "title=bad\\u0000title" in page
    assert re.search(r"title=bad[\x00-\x1f]title", page) is None


async def test_source_binding_does_not_touch_derived_indexing(tmp_path, monkeypatch):
    manager, bucket_id = await make_bucket(tmp_path)
    store = make_store(tmp_path)
    mock_index = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_index_after_update", mock_index)

    result = await attach(manager, store, bucket_id, "精确标题", "A\nB\n", [[1, 1]])
    assert result == f"source_attach ok bucket_id={bucket_id} slot=1 status=active ranges=1-1"
    assert mock_index.await_count == 0


async def test_active_limit_is_rejected_without_eviction(tmp_path):
    manager, bucket_id = await make_bucket(tmp_path)
    store = make_store(tmp_path)
    for index in range(MAX_SOURCE_REFS):
        content = f"内容 {index}\n"
        result = await attach(manager, store, bucket_id, "精确标题", content, [[1, 1]])
        assert result.startswith("source_attach ok")

    overflow = await attach(manager, store, bucket_id, "精确标题", "超出\n", [[1, 1]])
    assert overflow == f"source_attach 拒绝：活动 source_refs 上限 {MAX_SOURCE_REFS}。"

    def seed_ledger(post):
        links = []
        for index in range(MAX_SOURCE_LINKS):
            links.append({
                "ref": "src_" + (f"{index:064x}"),
                "ranges": [[1, 1]],
                "status": "detached",
            })
        post["source_links"] = links
        post["source_refs"] = []
        return True, "seeded"

    await manager.mutate_source_links(bucket_id, seed_ledger)
    overflow_ledger = await attach(manager, store, bucket_id, "精确标题", "再超出\n", [[1, 1]])
    assert overflow_ledger == f"source_attach 拒绝：source_links 上限 {MAX_SOURCE_LINKS}。"


async def test_archived_bucket_binding_is_reversible(tmp_path):
    manager, bucket_id = await make_bucket(tmp_path)
    store = make_store(tmp_path)

    assert await manager.archive(bucket_id) is True
    result = await attach(manager, store, bucket_id, "精确标题", "归档证据\n", [[1, 1]])
    assert result == f"source_attach ok bucket_id={bucket_id} slot=1 status=active ranges=1-1"
    assert await detach(manager, bucket_id, "精确标题", 1) == (
        f"source_detach ok bucket_id={bucket_id} slot=1 status=detached"
    )
    assert await restore(manager, bucket_id, "精确标题", 1) == (
        f"source_restore ok bucket_id={bucket_id} slot=1 status=active"
    )


async def test_no_hardlink_fallback_publishes_atomically(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    original_link = None
    import os

    original_link = os.link

    def fail_link(src, dst, **kwargs):
        raise OSError("no hard links")

    monkeypatch.setattr(os, "link", fail_link)

    ref = store.put("fallback\n")
    assert ref == "src_" + hashlib.sha256(b"fallback\n").hexdigest()
    assert (store.root / f"{ref}.source").read_text(encoding="utf-8") == "fallback\n"
