import pytest

from bucket_manager import BucketManager
from letter_service import letter_lock_update, letter_read, letter_write


@pytest.fixture
async def bucket_mgr(tmp_path):
    return BucketManager({"buckets_dir": str(tmp_path / "vault")})


async def test_letter_write_creates_locked_letter_with_writer_name(bucket_mgr):
    result = await letter_write(
        bucket_mgr,
        author="南枳",
        content="这是给你的锁信。",
        user_name="严槿",
        ai_name="南枳",
        title="先存一封信",
        lock_type="permanent",
        unlock_date="ignored",
    )
    assert result.startswith("💌letter→")
    assert "🔒permanent" in result
    bucket_id = result.split("💌letter→", 1)[1].split()[0]
    bucket = await bucket_mgr.get(bucket_id)
    metadata = bucket["metadata"]
    assert metadata["type"] == "letter"
    assert metadata["tags"] == ["__letter__"]
    assert metadata["importance"] == 10
    assert metadata["domain"] == ["letter"]
    assert metadata["author"] == "南枳"
    assert metadata["locked_by"] == "ai"
    assert metadata["writer_name"] == "南枳"
    assert metadata["unlock_date"] == "9999-12-31"


async def test_human_cannot_create_locked_letter_but_can_store_unlocked(bucket_mgr):
    with pytest.raises(ValueError, match="不能替对方创建带锁信"):
        await letter_write(
            bucket_mgr,
            author="user",
            content="human locked",
            user_name="严槿",
            ai_name="南枳",
            lock_type="timed",
            unlock_date="2099-01-01T00:00:00+08:00",
        )
    result = await letter_write(
        bucket_mgr,
        author="user",
        content="human unlocked",
        user_name="严槿",
        ai_name="南枳",
    )
    bucket_id = result.split("💌letter→", 1)[1].split()[0]
    bucket = await bucket_mgr.get(bucket_id)
    assert bucket["metadata"]["writer_name"] == "严槿"
    assert bucket["metadata"]["locked_by"] == ""


async def test_letter_lock_owner_and_cas_conflict_contract(bucket_mgr, monkeypatch):
    result = await letter_write(
        bucket_mgr,
        author="ai",
        content="locked owner test",
        ai_name="南枳",
        lock_type="timed",
        unlock_date="2099-01-01T00:00:00+08:00",
    )
    bucket_id = result.split("💌letter→", 1)[1].split()[0]
    human_change = await letter_lock_update(
        bucket_mgr,
        bucket_id,
        "none",
        "",
        caller_side="human",
    )
    assert human_change == "只有创建这把锁的一方可以修改 Letter 锁状态。"

    real_mutate_lock_fields = BucketManager.mutate_lock_fields

    async def race_lock_fields(manager, letter_id, mutation):
        async def concurrent_change(post):
            post["lock_type"] = "permanent"
            post["unlock_date"] = "9999-12-31"
            post["author"] = "human"
            return True, {"ok": True, "conflict": False}

        await real_mutate_lock_fields(manager, letter_id, concurrent_change)

        async def raced(post):
            return await mutation(post)

        return await real_mutate_lock_fields(manager, letter_id, raced)

    monkeypatch.setattr(BucketManager, "mutate_lock_fields", race_lock_fields)

    changed = await letter_lock_update(
        bucket_mgr,
        bucket_id,
        "timed",
        "2099-01-01T00:00:00+08:00",
    )
    assert changed == "Letter 锁状态已被并发修改，请重新读取后再试"


async def test_letter_read_hides_locked_content_until_expired(bucket_mgr):
    locked_result = await letter_write(
        bucket_mgr,
        author="ai",
        content="secret future content",
        ai_name="南枳",
        title="hidden",
        lock_type="permanent",
    )
    locked_id = locked_result.split("💌letter→", 1)[1].split()[0]
    unlocked_result = await letter_write(
        bucket_mgr,
        author="user",
        content="visible birthday letter",
        user_name="严槿",
        title="visible",
    )
    unlocked_id = unlocked_result.split("💌letter→", 1)[1].split()[0]

    locked = await letter_read(bucket_mgr, query="secret", caller_side="human")
    assert locked == "没有找到匹配的信件。"
    owner_view = await letter_read(bucket_mgr, query="secret")
    assert locked_id in owner_view
    assert "secret future content" in owner_view

    visible = await letter_read(bucket_mgr, query="visible", caller_side="human")
    assert unlocked_id in visible
    assert "visible birthday letter" in visible

    listed = await letter_read(bucket_mgr, caller_side="human")
    assert "一封上锁的信" in listed
    assert "secret future content" not in listed
