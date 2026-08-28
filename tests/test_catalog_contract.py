import pytest

from bucket_manager import BucketManager
from catalog import surface_catalog
from letter_service import letter_write
from relation_bindings import attach as relation_attach
from source_bindings import attach as source_attach
from source_store import SourceStore


@pytest.fixture
async def bucket_mgr(tmp_path):
    return BucketManager({"buckets_dir": str(tmp_path / "vault")})


async def test_catalog_groups_metadata_without_letter_content(bucket_mgr):
    source_store = SourceStore(bucket_mgr.base_dir)
    relation_target_id = await bucket_mgr.create(
        "relation target",
        tags=["target"],
        domain=["career"],
        importance=7,
        bucket_type="dynamic",
        name="Relation target",
    )
    target_id = await bucket_mgr.create(
        "dynamic target",
        tags=["target"],
        domain=["career"],
        importance=7,
        bucket_type="dynamic",
        name="Target",
        extra_metadata={"title": "Target"},
    )
    await bucket_mgr.create(
        "permanent body",
        tags=["root"],
        domain=["home"],
        importance=9,
        bucket_type="permanent",
        name="Pinned rule",
        pinned=True,
        protected=True,
    )
    await source_attach(bucket_mgr, source_store, target_id, "Target", "source text", [[1, 1]])
    await relation_attach(bucket_mgr, target_id, relation_target_id, "causes")
    await bucket_mgr.create(
        "dynamic body",
        tags=["root", "work"],
        domain=["career"],
        importance=6,
        bucket_type="dynamic",
        name="Work thread",
        anchor=True,
    )
    await bucket_mgr.create(
        "feel body",
        tags=["soft"],
        domain=["mood"],
        importance=8,
        bucket_type="feel",
        name="Quiet feeling",
    )
    await letter_write(
        bucket_mgr,
        author="ai",
        content="locked body",
        ai_name="南枳",
        title="hidden title",
        lock_type="permanent",
    )

    output = await surface_catalog(bucket_mgr)

    assert output.startswith("=== 记忆目录（6 桶）===")
    assert "--- 固化（1）---" in output
    assert "--- 动态（3）---" in output
    assert "source_available:true" in output
    assert "causes" in output or "结果" in output
    assert "--- feel（1）---" in output
    assert "--- letter（1）---" in output
    assert "📌🛡️ [受保护记忆] Pinned rule | home | 10" in output
    assert "⚓ [anchor] Work thread | career | 6" in output
    assert "Quiet feeling | mood | 8" in output
    assert "一封上锁的信 | letter | 10" in output
    assert "Pinned rule body" not in output
    assert "dynamic body" not in output
    assert "feel body" not in output
    assert "locked body" not in output
    assert "hidden title" not in output


async def test_catalog_filters_by_domain_tags_and_limit(bucket_mgr):
    await bucket_mgr.create(
        "home memory",
        tags=["home", "root"],
        domain=["home"],
        importance=10,
        bucket_type="permanent",
        name="Home memory",
    )
    await bucket_mgr.create(
        "work memory",
        tags=["work"],
        domain=["career"],
        importance=9,
        bucket_type="dynamic",
        name="Work memory",
    )
    await bucket_mgr.create(
        "low memory",
        tags=["home", "root"],
        domain=["home"],
        importance=4,
        bucket_type="dynamic",
        name="Low memory",
    )

    filtered = await surface_catalog(
        bucket_mgr,
        domain_filter=["home"],
        tag_filter=["root"],
    )
    assert "Home memory" in filtered
    assert "Low memory" in filtered
    assert "Work memory" not in filtered

    limited = await surface_catalog(bucket_mgr, domain_filter=["home"], max_results=1)
    assert "Home memory" in limited
    assert "Low memory" not in limited
