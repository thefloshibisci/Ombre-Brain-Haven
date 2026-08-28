import pytest
from pathlib import Path

from pathlib import Path

from bucket_manager import BucketManager


@pytest.fixture
async def bucket_mgr(tmp_path):
    return BucketManager({"buckets_dir": str(tmp_path / "vault")})


async def test_only_creation_marked_test_bucket_can_be_hard_deleted(bucket_mgr):
    real_id = await bucket_mgr.create(content="a real memory", domain=["life"])
    test_id = await bucket_mgr.create(
        content="synthetic memory for a test",
        domain=["test"],
        source="hold",
        test_data=True,
    )

    test_bucket = await bucket_mgr.get(test_id)
    assert test_bucket["metadata"]["provenance"] == {
        "kind": "test",
        "created_by": "hold",
        "erasable": True,
    }

    refused = await bucket_mgr.hard_delete_test_bucket(real_id, reason="should fail")
    assert refused == {"ok": False, "error": "not_erasable_test_data"}
    assert await bucket_mgr.get(real_id) is not None

    erased_path = Path(test_bucket["path"])
    erased = await bucket_mgr.hard_delete_test_bucket(test_id, reason="test cleanup")
    assert erased == {"ok": True, "deleted": test_id}
    assert not erased_path.exists()
    assert await bucket_mgr.get(test_id) is None


async def test_test_data_cleanup_requires_reason(bucket_mgr):
    test_id = await bucket_mgr.create(
        content="synthetic gateway test payload",
        domain=["test"],
        source="hold",
        test_data=True,
    )
    test_bucket = await bucket_mgr.get(test_id)
    test_path = Path(test_bucket["path"])

    missing = await bucket_mgr.hard_delete_test_bucket(test_id, reason="   ")
    assert missing == {"ok": False, "error": "missing_delete_reason"}
    assert test_path.exists()

    too_long = await bucket_mgr.hard_delete_test_bucket(test_id, reason="x" * 501)
    assert too_long == {"ok": False, "error": "delete_reason_too_long"}
    assert test_path.exists()

    deleted = await bucket_mgr.hard_delete_test_bucket(test_id, reason="test cleanup")
    assert deleted == {"ok": True, "deleted": test_id}
    assert not test_path.exists()

    tombstone_path = Path(bucket_mgr.tombstone_dir) / f"{test_id}.json"
    assert tombstone_path.exists()
