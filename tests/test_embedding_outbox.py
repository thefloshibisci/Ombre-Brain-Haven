import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from embedding_outbox import EmbeddingOutbox, content_hash


def _config(tmp_path, **embedding):
    return {
        "buckets_dir": str(tmp_path / "vault"),
        "embedding": {
            "enabled": True,
            "background_indexing": True,
            "retry_base_seconds": 0.01,
            "retry_max_seconds": 0.02,
            **embedding,
        },
    }


async def _wait_for(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


class RecordingEngine:
    enabled = True

    def __init__(self):
        self.calls = []
        self.hashes = {}

    async def generate_and_store(self, bucket_id, content):
        self.calls.append((bucket_id, content))
        self.hashes[bucket_id] = content_hash(content)
        return True

    def list_all_ids(self):
        return list(self.hashes)

    def list_content_ids(self):
        return [bucket_id for bucket_id, digest in self.hashes.items() if digest]

    def list_content_hashes(self):
        return dict(self.hashes)

    def delete_embedding(self, bucket_id):
        self.hashes.pop(bucket_id, None)


class BlockingEngine(RecordingEngine):
    def __init__(self, *, block_first=True):
        super().__init__()
        self.block_first = block_first
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate_and_store(self, bucket_id, content):
        self.calls.append((bucket_id, content))
        if self.block_first and len(self.calls) == 1:
            self.started.set()
            await self.release.wait()
        self.hashes[bucket_id] = content_hash(content)
        return True


class FailingEngine(RecordingEngine):
    async def generate_and_store(self, bucket_id, content):
        self.calls.append((bucket_id, content))
        return False


class DisabledEngine(RecordingEngine):
    enabled = False


class ObservableEngine(RecordingEngine):
    def __init__(self):
        super().__init__()
        self.manager = None
        self.meaning_started = asyncio.Event()
        self.release_meaning = asyncio.Event()
        self.visible_during_meaning = None

    async def get_embedding(self, bucket_id):
        return [0.1, 0.2, 0.3] if bucket_id in self.hashes else None

    async def generate_and_store_meaning(self, bucket_id, _meaning):
        self.visible_during_meaning = (
            await self.manager.get(bucket_id) is not None
        )
        self.meaning_started.set()
        await self.release_meaning.wait()
        return True


@pytest.mark.asyncio
async def test_stale_empty_reconcile_keeps_new_pending_item(tmp_path):
    config = _config(tmp_path)
    engine = RecordingEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)

    assert outbox.enqueue("fresh-id", "fresh content") is True
    await outbox.reconcile(buckets=[], include_archive=False)

    assert outbox.is_pending("fresh-id") is True


@pytest.mark.asyncio
async def test_stale_content_reconcile_never_overwrites_newer_pending_hash(
    tmp_path,
):
    config = _config(tmp_path)
    engine = RecordingEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)
    bucket_id = "same-id"
    old_content = "old content snapshot"
    new_content = "new content already queued"
    engine.hashes[bucket_id] = content_hash(old_content)
    assert outbox.enqueue(bucket_id, new_content) is True

    await outbox.reconcile(
        buckets=[{
            "id": bucket_id,
            "content": old_content,
            "metadata": {},
        }],
        include_archive=False,
    )

    assert outbox.is_pending(bucket_id) is True
    assert outbox._items[bucket_id]["content_hash"] == content_hash(
        new_content
    )


@pytest.mark.asyncio
async def test_reconcile_does_not_requeue_a_worker_completed_mid_scan(tmp_path):
    config = _config(tmp_path)
    bucket_id = "completed-during-reconcile"
    content = "the worker already stored this exact content"
    digest = content_hash(content)

    class Manager:
        async def get(self, requested_id):
            assert requested_id == bucket_id
            return {"id": bucket_id, "content": content, "metadata": {}}

    class CompletingEngine(RecordingEngine):
        def __init__(self):
            super().__init__()
            self.outbox = None
            self.completed = False

        def list_content_hashes(self):
            if not self.completed:
                self.completed = True
                self.hashes[bucket_id] = digest
                self.outbox._complete(bucket_id, digest)
            return dict(self.hashes)

    engine = CompletingEngine()
    outbox = EmbeddingOutbox(config, Manager(), engine)
    engine.outbox = outbox
    assert outbox.enqueue(bucket_id, content) is True

    queued = await outbox.reconcile(
        buckets=[{"id": bucket_id, "content": content, "metadata": {}}],
        include_archive=False,
    )

    assert queued == 0
    assert outbox.is_pending(bucket_id) is False


@pytest.mark.asyncio
async def test_reconcile_refreshes_stale_bucket_content_before_queueing(tmp_path):
    config = _config(tmp_path)
    bucket_id = "edited-after-caller-snapshot"
    old_content = "old caller snapshot"
    new_content = "new Markdown truth already indexed"
    engine = RecordingEngine()
    engine.hashes[bucket_id] = content_hash(new_content)

    class Manager:
        async def get(self, requested_id):
            assert requested_id == bucket_id
            return {"id": bucket_id, "content": new_content, "metadata": {}}

    outbox = EmbeddingOutbox(config, Manager(), engine)
    queued = await outbox.reconcile(
        buckets=[{"id": bucket_id, "content": old_content, "metadata": {}}],
        include_archive=False,
    )

    assert queued == 0
    assert outbox.status()["pending"] == 0


def test_ensure_pending_never_overwrites_newer_content(tmp_path):
    config = _config(tmp_path)
    engine = RecordingEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)
    bucket_id = "repair-cas"
    new_content = "newer content already queued"

    assert outbox.enqueue(bucket_id, new_content) is True
    assert outbox.ensure_pending(bucket_id, "stale repair content") is True

    assert outbox._items[bucket_id]["content_hash"] == content_hash(
        new_content
    )


@pytest.mark.asyncio
async def test_reconcile_queues_content_for_meaning_only_index_row(tmp_path):
    config = _config(tmp_path)
    engine = RecordingEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)
    bucket_id = "meaning-only"
    engine.hashes[bucket_id] = ""

    async def get_bucket(requested_id):
        assert requested_id == bucket_id
        return {
            "id": bucket_id,
            "content": "正文仍然需要自己的向量",
            "metadata": {"meaning": ["独立的 meaning"]},
        }

    manager.get = get_bucket

    queued = await outbox.reconcile(
        buckets=[{
            "id": bucket_id,
            "content": "正文仍然需要自己的向量",
            "metadata": {"meaning": ["独立的 meaning"]},
        }],
        include_archive=False,
    )

    assert queued == 1
    assert outbox.is_pending(bucket_id) is True


@pytest.mark.asyncio
async def test_reconcile_index_read_failure_does_not_queue_whole_vault(
    tmp_path,
):
    config = _config(tmp_path)

    class BrokenIndexEngine(RecordingEngine):
        content_reader_called = False

        def list_content_ids(self):
            self.content_reader_called = True
            raise sqlite3.OperationalError("database is busy")

    engine = BrokenIndexEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)

    queued = await outbox.reconcile(buckets=[{
        "id": "existing",
        "content": "must not trigger a full reindex storm",
        "metadata": {},
    }])

    assert queued == 0
    assert engine.content_reader_called is True
    assert outbox.status()["pending"] == 0


@pytest.mark.asyncio
async def test_reconcile_hash_read_failure_falls_back_to_index_ids(tmp_path):
    config = _config(tmp_path)

    class BrokenHashEngine(RecordingEngine):
        def list_content_hashes(self):
            raise sqlite3.OperationalError("database is busy")

    engine = BrokenHashEngine()
    bucket_id = "already-indexed"
    engine.hashes[bucket_id] = content_hash("stored content")
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)

    queued = await outbox.reconcile(buckets=[{
        "id": bucket_id,
        "content": "stored content",
        "metadata": {},
    }])

    assert queued == 0
    assert outbox.status()["pending"] == 0


@pytest.mark.asyncio
async def test_transient_failure_survives_stale_reconcile_and_recovers(
    tmp_path,
):
    config = _config(tmp_path)

    class RecoveringEngine(RecordingEngine):
        failing = True

        async def generate_and_store(self, bucket_id, content):
            self.calls.append((bucket_id, content))
            if self.failing:
                return False
            self.hashes[bucket_id] = content_hash(content)
            return True

    engine = RecoveringEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)
    manager.attach_embedding_outbox(outbox)
    outbox._running = True
    bucket_id = await manager.create(content="retry after stale reconcile")
    outbox._running = False

    assert await outbox.process_once() is True
    assert outbox.status()["retrying"] == 1
    attempts = outbox._items[bucket_id]["attempts"]

    await outbox.reconcile(buckets=[], include_archive=False)
    assert outbox.is_pending(bucket_id) is True
    assert outbox._items[bucket_id]["attempts"] == attempts

    engine.failing = False
    outbox.retry_now()
    assert await outbox.process_once() is True
    assert outbox.is_pending(bucket_id) is False
    assert engine.hashes[bucket_id] == content_hash(
        "retry after stale reconcile"
    )


@pytest.mark.asyncio
async def test_hold_create_queues_content_and_meaning_without_second_worker_pass(
    tmp_path,
):
    """A durable write must queue both components while Markdown is readable."""
    config = _config(tmp_path)
    engine = ObservableEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    engine.manager = manager
    outbox = EmbeddingOutbox(config, manager, engine)
    manager.attach_embedding_outbox(outbox)

    await outbox.start(reconcile=False)
    task = asyncio.create_task(
        manager.create(
            content="hold body with a separate meaning vector",
            meaning="why this memory matters",
        )
    )
    try:
        await asyncio.wait_for(engine.meaning_started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        engine.release_meaning.set()
        bucket_id = await asyncio.wait_for(task, timeout=1)
        assert await outbox.wait_until_idle(timeout=1)
    finally:
        engine.release_meaning.set()
        if not task.done():
            task.cancel()
        await outbox.stop()

    bucket = await manager.get(bucket_id)
    assert bucket is not None
    assert bucket["metadata"]["meaning"] == ["why this memory matters"]
    assert engine.visible_during_meaning is True
    assert engine.calls == [
        (bucket_id, "hold body with a separate meaning vector")
    ]
    assert await engine.get_embedding(bucket_id) is not None
    assert outbox.status()["pending"] == 0
    assert outbox.status()["retrying"] == 0


@pytest.mark.asyncio
async def test_background_indexing_never_blocks_markdown_write(tmp_path):
    config = _config(tmp_path)
    engine = BlockingEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)
    manager.attach_embedding_outbox(outbox)

    await outbox.start(reconcile=False)
    try:
        bucket_id = await asyncio.wait_for(
            manager.create(content="memory survives a slow provider"),
            timeout=0.2,
        )
        bucket = await manager.get(bucket_id)

        assert bucket is not None
        assert bucket["content"] == "memory survives a slow provider"
        assert outbox.is_pending(bucket_id)
        await asyncio.wait_for(engine.started.wait(), timeout=0.5)

        engine.release.set()
        assert await outbox.wait_until_idle(timeout=1.0)
        assert engine.hashes[bucket_id] == content_hash(bucket["content"])
    finally:
        engine.release.set()
        await outbox.stop()


@pytest.mark.asyncio
async def test_create_returns_while_embedding_worker_is_blocked(tmp_path):
    """Markdown visibility must not wait for a slow embedding provider."""
    config = _config(tmp_path)
    engine = BlockingEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)
    manager.attach_embedding_outbox(outbox)
    content = "grow returns before a slow embedding provider"

    await outbox.start(reconcile=False)
    try:
        bucket_id = await asyncio.wait_for(
            manager.create(content=content),
            timeout=0.2,
        )
        bucket = await manager.get(bucket_id)

        assert bucket is not None
        assert bucket["content"] == content
        assert outbox.is_pending(bucket_id)
        await asyncio.wait_for(engine.started.wait(), timeout=0.5)
    finally:
        engine.release.set()
        assert await outbox.wait_until_idle(timeout=1.0)
        await outbox.stop()


@pytest.mark.asyncio
async def test_retry_state_survives_restart_and_recovers(tmp_path):
    config = _config(tmp_path)
    failing = FailingEngine()
    manager = BucketManager(config)
    manager.embedding_engine = failing
    outbox = EmbeddingOutbox(config, manager, failing)
    manager.attach_embedding_outbox(outbox)

    await outbox.start(reconcile=False)
    bucket_id = await manager.create(content="retry me after restart")
    await _wait_for(lambda: outbox.status()["retrying"] == 1)
    await outbox.stop()

    payload = json.loads((tmp_path / "vault" / ".embedding_outbox.json").read_text("utf-8"))
    assert payload["items"][bucket_id]["attempts"] >= 1

    recovered = RecordingEngine()
    restarted = EmbeddingOutbox(config, manager, recovered)
    manager.embedding_engine = recovered
    manager.attach_embedding_outbox(restarted)
    await restarted.start(reconcile=False)
    try:
        assert await restarted.wait_until_idle(timeout=1.0)
        assert recovered.calls == [(bucket_id, "retry me after restart")]
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_content_changed_during_indexing_is_requeued(tmp_path):
    config = _config(tmp_path)
    engine = BlockingEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)
    manager.attach_embedding_outbox(outbox)

    await outbox.start(reconcile=False)
    try:
        bucket_id = await manager.create(content="old content")
        await asyncio.wait_for(engine.started.wait(), timeout=0.5)
        assert await manager.update(bucket_id, content="new content")

        engine.release.set()
        assert await outbox.wait_until_idle(timeout=1.0)
        assert [content for _bucket_id, content in engine.calls] == [
            "old content",
            "new content",
        ]
        assert engine.hashes[bucket_id] == content_hash("new content")
    finally:
        engine.release.set()
        await outbox.stop()


@pytest.mark.asyncio
async def test_all_memory_types_persist_while_embedding_is_disabled(tmp_path):
    config = _config(tmp_path, enabled=False)
    engine = DisabledEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)
    manager.attach_embedding_outbox(outbox)

    await outbox.start(reconcile=False)
    try:
        ids = []
        for bucket_type in ("dynamic", "permanent", "feel"):
            ids.append(
                await manager.create(
                    content=f"offline {bucket_type}",
                    bucket_type=bucket_type,
                )
            )

        assert outbox.status()["pending"] == len(ids)
        for bucket_id in ids:
            assert await manager.get(bucket_id) is not None
        assert engine.calls == []
    finally:
        await outbox.stop()


@pytest.mark.asyncio
async def test_provider_circuit_breaker_stops_failure_storm_and_recovers(tmp_path):
    config = _config(
        tmp_path,
        circuit_failure_threshold=2,
        circuit_base_seconds=5,
        circuit_max_seconds=5,
    )
    failing = FailingEngine()
    manager = BucketManager(config)
    manager.embedding_engine = failing
    outbox = EmbeddingOutbox(config, manager, failing)
    manager.attach_embedding_outbox(outbox)

    await outbox.start(reconcile=False)
    try:
        bucket_ids = [
            await manager.create(content=f"circuit memory {index}")
            for index in range(4)
        ]
        await _wait_for(lambda: outbox.status()["circuit"]["state"] == "open")
        calls_at_trip = len(failing.calls)
        await asyncio.sleep(0.05)

        assert calls_at_trip == 2
        assert len(failing.calls) == calls_at_trip
        assert outbox.status()["pending"] == 4
        assert outbox.status()["circuit"]["trips"] == 1

        recovered = RecordingEngine()
        manager.embedding_engine = recovered
        outbox.set_embedding_engine(recovered)
        outbox.retry_now()

        assert await outbox.wait_until_idle(timeout=1.0)
        assert {bucket_id for bucket_id, _content in recovered.calls} == set(bucket_ids)
        assert outbox.status()["circuit"]["state"] == "closed"
    finally:
        await outbox.stop()


@pytest.mark.asyncio
async def test_poison_item_does_not_trip_circuit_or_block_other_items(tmp_path):
    """回归锁死找茬会话发现的 bug：一条内容永久失败的桶反复重试，

    绝不能把熔断器跳到全局打开，连累队列里所有其他合法待处理的记忆一起
    卡住（原来的实现把「同一个桶重试很多次」和「供应商真的挂了」算成同一
    件事，都计入熔断计数）。
    """
    config = _config(
        tmp_path,
        circuit_failure_threshold=2,
        circuit_base_seconds=5,
        circuit_max_seconds=5,
    )
    poison_content = "这条内容永远生成不出向量"

    class LazyPoisonEngine(RecordingEngine):
        async def generate_and_store(self, bucket_id, content):
            self.calls.append((bucket_id, content))
            if content == poison_content:
                return False
            self.hashes[bucket_id] = content_hash(content)
            return True

    engine = LazyPoisonEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)
    manager.attach_embedding_outbox(outbox)

    await outbox.start(reconcile=False)
    try:
        await manager.create(content=poison_content)

        # 毒药桶自己反复重试很多次（远超 circuit_failure_threshold=2），
        # 熔断绝不能因此打开。
        await _wait_for(lambda: len(engine.calls) >= 5, timeout=2.0)
        assert outbox.status()["circuit"]["state"] == "closed"
        assert outbox.status()["circuit"]["trips"] == 0

        # 熔断没开着，新写入的合法记忆必须正常被处理，不会陪毒药桶一起卡住。
        good_id = await manager.create(content="一条完全正常的记忆")
        await _wait_for(
            lambda: any(bid == good_id for bid, _c in engine.calls), timeout=1.0
        )
        assert good_id in engine.hashes
    finally:
        await outbox.stop()


@pytest.mark.asyncio
async def test_cancel_after_meaning_commit_survives_outbox_reload(
    tmp_path,
    monkeypatch,
):
    """请求在 Markdown 提交后取消，meaning 期望状态仍可由新 loop/进程恢复。"""
    config = _config(tmp_path)

    class MeaningEngine(RecordingEngine):
        def __init__(self):
            super().__init__()
            self.meaning_calls = []

        async def generate_and_store_meaning(self, bucket_id, meaning):
            self.meaning_calls.append((bucket_id, meaning))
            return True

        def delete_meaning_embedding(self, bucket_id):
            self.meaning_calls.append((bucket_id, ""))

    engine = MeaningEngine()
    manager = BucketManager(config)
    manager.embedding_engine = engine
    bucket_id = await manager.create("durable meaning base")
    outbox = EmbeddingOutbox(config, manager, engine)
    manager.attach_embedding_outbox(outbox)

    async def cancel_after_commit(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(manager, "_index_after_update", cancel_after_commit)
    with pytest.raises(asyncio.CancelledError):
        await manager.update(bucket_id, meaning=["survives request loop"])

    persisted = json.loads(
        (tmp_path / "vault" / ".embedding_outbox.json").read_text("utf-8")
    )
    pending = persisted["items"][bucket_id]
    assert pending.get("meaning_hash") == content_hash("survives request loop")
    assert "survives request loop" not in json.dumps(persisted, ensure_ascii=False)

    restarted = EmbeddingOutbox(config, manager, engine)
    assert await restarted.process_once() is True
    assert engine.meaning_calls == [(bucket_id, "survives request loop")]
    assert restarted.is_pending(bucket_id) is False


@pytest.mark.asyncio
async def test_meaning_failure_does_not_starve_content_component(tmp_path):
    bucket_id = "two-components"
    content = "content must still be indexed"
    meaning = "provider rejects this meaning"

    class Manager:
        async def get(self, requested_id):
            assert requested_id == bucket_id
            return {
                "id": bucket_id,
                "content": content,
                "metadata": {"meaning": [meaning]},
            }

    class ComponentEngine(RecordingEngine):
        def __init__(self):
            super().__init__()
            self.meaning_calls = []

        async def generate_and_store_meaning(self, requested_id, text):
            self.meaning_calls.append((requested_id, text))
            return False

    engine = ComponentEngine()
    outbox = EmbeddingOutbox(
        _config(
            tmp_path,
            retry_base_seconds=60,
            retry_max_seconds=60,
            circuit_failure_threshold=99,
        ),
        Manager(),
        engine,
    )
    outbox.enqueue_meaning(bucket_id, meaning)
    outbox.enqueue(bucket_id, content)

    meaning_item = dict(outbox._items[bucket_id])
    meaning_item["_component_kind"] = "meaning"
    await outbox._process(bucket_id, meaning_item, engine)
    failed = dict(outbox._items[bucket_id])
    meaning_due = failed["meaning_next_attempt_at"]
    assert failed["meaning_attempts"] == 1
    assert meaning_due > time.time()

    # The content component is independently due even though meaning is in a
    # long retry backoff. Processing content must not reset meaning's failure.
    assert await outbox.process_once() is True
    assert engine.calls == [(bucket_id, content)]
    assert engine.meaning_calls == [(bucket_id, meaning)]
    remaining = outbox._items[bucket_id]
    assert "content_hash" not in remaining
    assert remaining["meaning_hash"] == content_hash(meaning)
    assert remaining["meaning_attempts"] == 1
    assert remaining["meaning_next_attempt_at"] == meaning_due


def test_multiple_instances_merge_whole_file_updates_and_component_cas(tmp_path):
    config = _config(tmp_path)
    engine = RecordingEngine()

    class Manager:
        pass

    first = EmbeddingOutbox(config, Manager(), engine)
    second = EmbeddingOutbox(config, Manager(), engine)

    # Both instances loaded the same empty snapshot before either write. Every
    # RMW must reload under the shared sidecar lease instead of replacing the
    # other instance's item set.
    first.enqueue("content-only", "first content")
    second.enqueue_meaning("meaning-only", "first meaning")
    second.enqueue_meaning("combined", "combined meaning")
    first.enqueue("combined", "combined content")

    reloaded = EmbeddingOutbox(config, Manager(), engine)
    assert reloaded.pending_ids() == {
        "content-only",
        "meaning-only",
        "combined",
    }
    combined = reloaded._items["combined"]
    assert combined["content_hash"] == content_hash("combined content")
    assert combined["meaning_hash"] == content_hash("combined meaning")

    # A stale instance completing one component and another instance failing
    # its sibling must merge both CAS mutations without resurrecting or
    # deleting unrelated work.
    first.complete_content("combined", "combined content")
    second._fail_component(
        "combined",
        content_hash("combined meaning"),
        "meaning failed",
        "meaning",
    )
    final = EmbeddingOutbox(config, Manager(), engine)
    assert final.pending_ids() == {
        "content-only",
        "meaning-only",
        "combined",
    }
    combined = final._items["combined"]
    assert "content_hash" not in combined
    assert combined["meaning_hash"] == content_hash("combined meaning")
    assert combined["meaning_attempts"] == 1


@pytest.mark.asyncio
async def test_old_instance_observes_and_processes_new_instance_task(tmp_path):
    bucket_id = "written-by-new-instance"
    content = "durable shared task"

    class Manager:
        async def get(self, requested_id):
            assert requested_id == bucket_id
            return {"id": bucket_id, "content": content, "metadata": {}}

    config = _config(tmp_path)
    engine = RecordingEngine()
    old = EmbeddingOutbox(config, Manager(), engine)
    new = EmbeddingOutbox(config, Manager(), engine)
    new.enqueue(bucket_id, content)

    assert old.status()["pending"] == 1
    assert await old.process_once() is True
    assert engine.calls == [(bucket_id, content)]
    assert EmbeddingOutbox(config, Manager(), engine).pending_ids() == set()


@pytest.mark.asyncio
async def test_worker_revalidates_component_inside_derived_turn(tmp_path):
    bucket_id = "stale-worker"
    content = "already completed elsewhere"

    class Manager:
        def __init__(self):
            self.turns = 0

        @asynccontextmanager
        async def _derived_index_turn(self, requested_id):
            assert requested_id == bucket_id
            self.turns += 1
            yield

        async def get(self, _requested_id):
            pytest.fail("stale work must be rejected before reading the bucket")

    config = _config(tmp_path)
    engine = RecordingEngine()
    manager = Manager()
    outbox = EmbeddingOutbox(config, manager, engine)
    outbox.enqueue(bucket_id, content)
    _, stale_item, _ = outbox._next_due()
    assert stale_item is not None

    other = EmbeddingOutbox(config, manager, engine)
    other.complete_content(bucket_id, content)
    await outbox._process(bucket_id, stale_item, engine)

    assert manager.turns == 1
    assert engine.calls == []
    assert outbox.status()["pending"] == 0


def test_v2_shared_retry_state_migrates_to_each_pending_component(tmp_path):
    config = _config(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    queued_at = "2026-01-01T00:00:00"
    retry_at = time.time() + 30
    (vault / ".embedding_outbox.json").write_text(
        json.dumps(
            {
                "version": 2,
                "items": {
                    "legacy": {
                        "content_hash": content_hash("legacy content"),
                        "meaning_hash": content_hash("legacy meaning"),
                        "queued_at": queued_at,
                        "updated_at": queued_at,
                        "attempts": 2,
                        "next_attempt_at": retry_at,
                        "last_attempt_at": queued_at,
                        "last_error": "legacy failure",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    outbox = EmbeddingOutbox(config, object(), RecordingEngine())
    item = outbox._items["legacy"]
    for kind in ("content", "meaning"):
        assert item[f"{kind}_attempts"] == 2
        assert item[f"{kind}_next_attempt_at"] == retry_at
        assert item[f"{kind}_last_error"] == "legacy failure"


def test_embedding_schema_migrates_and_records_content_hash(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    db_path = vault / "embeddings.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO embeddings VALUES (?, ?, ?)",
            ("legacy", "[0.1]", "2026-01-01T00:00:00Z"),
        )

    engine = EmbeddingEngine({
        "buckets_dir": str(vault),
        "embedding": {"enabled": False},
    })
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(embeddings)")}
    assert "content_hash" in columns
    assert engine.get_content_hash("legacy") == ""
    assert engine.list_content_ids() == ["legacy"]

    digest = hashlib.sha256(b"current content").hexdigest()
    engine._store_embedding("legacy", [0.2, 0.3], digest)
    assert engine.get_content_hash("legacy") == digest


@pytest.mark.asyncio
async def test_reconcile_distinguishes_legacy_vector_from_meaning_only_row(
    tmp_path,
):
    vault = tmp_path / "vault"
    vault.mkdir()
    db_path = vault / "embeddings.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE embeddings (
                bucket_id TEXT PRIMARY KEY,
                embedding TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO embeddings VALUES (?, ?, ?)",
            ("legacy-content", "[0.1]", "2026-01-01T00:00:00Z"),
        )

    config = {
        "buckets_dir": str(vault),
        "embedding": {"enabled": False, "background_indexing": True},
    }
    engine = EmbeddingEngine(config)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO embeddings
               (bucket_id, embedding, updated_at, content_hash, meaning_embedding)
               VALUES (?, '', ?, '', ?)""",
            ("meaning-only", "2026-01-01T00:00:00Z", "[0.2]"),
        )
    manager = BucketManager(config)
    manager.embedding_engine = engine
    outbox = EmbeddingOutbox(config, manager, engine)

    async def get_bucket(bucket_id):
        return {
            "id": bucket_id,
            "content": f"current content for {bucket_id}",
            "metadata": {},
        }

    manager.get = get_bucket
    queued = await outbox.reconcile(
        buckets=[
            {
                "id": "legacy-content",
                "content": "current content for legacy-content",
                "metadata": {},
            },
            {
                "id": "meaning-only",
                "content": "current content for meaning-only",
                "metadata": {"meaning": ["separate meaning"]},
            },
        ],
        include_archive=False,
    )

    assert engine.list_content_ids() == ["legacy-content"]
    assert queued == 1
    assert outbox.pending_ids() == {"meaning-only"}

