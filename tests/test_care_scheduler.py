import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

# Gateway imports its root-level dependencies inside the existing isolation boundary.
import gateway
import care_scheduler
from care_scheduler import CareScheduler


@pytest.fixture
def care_config(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("OMBRE_"):
            monkeypatch.delenv(name, raising=False)
    return {
        "buckets_dir": str(tmp_path / "buckets"),
        "state_dir": str(tmp_path / "state"),
        "embedding": {"enabled": False},
        "dehydration": {},
        "persona": {"enabled": False},
        "reflection": {
            "enabled": True, "auto_enabled": True, "daily_enabled": True,
            "daily_hour": 4, "daily_min_memory_items": 1,
            "daily_chat_memory_mode": "off", "daily_activity_summary_enabled": False,
            "diary_memory_extract_enabled": False,
            "api_key": "test-only", "base_url": "https://care.invalid", "model": "test",
        },
        "portrait": {"enabled": False},
        "dream": {"enabled": False},
    }


def make_service(config):
    return SimpleNamespace(
        config=config,
        bucket_mgr=gateway.BucketManager(config),
        persona_engine=None,
        embedding_engine=None,
        state_store=None,
        raw_event_store=None,
    )


@pytest.mark.asyncio
async def test_daily_generation_is_readable_by_current_brain_and_idempotent(care_config, monkeypatch):
    service = make_service(care_config)
    from bucket_manager import BucketManager as CurrentBucketManager
    from web.legacy_compat import _moment

    current = CurrentBucketManager(care_config)
    current.external_change_poll_seconds = 0
    assert await current.list_all() == []
    await service.bucket_mgr.create(
        bucket_id="care_source", content="We finished the violet observatory together.",
        created="2026-09-10T12:00:00+08:00",
    )
    engine_type = care_scheduler.ReflectionEngine
    frozen = datetime(2026, 9, 11, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    original_now = engine_type._local_now
    monkeypatch.setattr(engine_type, "_local_now", lambda self, now=None: original_now(self, now or frozen))
    generate = AsyncMock(return_value={"title": "Shared work", "content": "I felt close as we finished the observatory."})
    monkeypatch.setattr(engine_type, "_api_reflect", generate)
    scheduler = CareScheduler(service)

    await scheduler.run_once("reflection")
    first = scheduler.status()["jobs"]["reflection"]
    assert first["results"][0]["status"] == "created"
    await scheduler.run_once("reflection")
    assert scheduler.status()["jobs"]["reflection"]["results"][0]["status"] == "exists"
    assert generate.await_count == 1

    assert any(item["id"] == "reflection_daily_2026-09-10" for item in await current.list_all())
    bucket = await current.get("reflection_daily_2026-09-10")
    assert bucket["content"] == "I felt close as we finished the observatory."
    assert _moment(bucket)["date"] == "2026-09-10"
    assert "I felt close" not in json.dumps(first)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, expected", [(False, "insufficient_daily_memory"), (True, "generator_error")])
async def test_no_fabricated_memory_when_materials_missing_or_model_fails(care_config, monkeypatch, failure, expected):
    service = make_service(care_config)
    if failure:
        await service.bucket_mgr.create(content="A source memory.", created="2026-09-10T12:00:00+08:00")
    original = care_scheduler.ReflectionEngine._local_now
    monkeypatch.setattr(care_scheduler.ReflectionEngine, "_local_now", lambda self, now=None: original(
        self, now or datetime(2026, 9, 11, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    ))
    generate = AsyncMock(side_effect=RuntimeError("provider offline"))
    monkeypatch.setattr(care_scheduler.ReflectionEngine, "_api_reflect", generate)
    scheduler = CareScheduler(service)
    await scheduler.run_once("reflection")
    result = scheduler.status()["jobs"]["reflection"]["results"][0]
    assert result["status"] == "skipped"
    assert result["reason"] == expected
    assert await service.bucket_mgr.get("reflection_daily_2026-09-10") is None
    assert generate.await_count == int(failure)


@pytest.mark.asyncio
async def test_disabled_job_does_not_construct_engine_and_config_changes_take_effect(care_config, monkeypatch):
    service = make_service(care_config)
    care_config["reflection"]["auto_enabled"] = False
    factory = AsyncMock()
    monkeypatch.setitem(care_scheduler.ENGINE_TYPES, "reflection", factory)
    scheduler = CareScheduler(service)
    await scheduler.run_once("reflection")
    factory.assert_not_called()
    assert scheduler.status()["jobs"]["reflection"]["results"] == [{"status": "disabled"}]
    care_config["reflection"]["auto_enabled"] = True
    # A broken engine must be visible, then the following interval can recover.
    def broken(_):
        raise ValueError("secret-and-memory-must-not-be-logged")
    monkeypatch.setitem(care_scheduler.ENGINE_TYPES, "reflection", broken)
    await scheduler.run_once("reflection")
    assert scheduler.status()["jobs"]["reflection"]["reason"] == "ValueError"
    care_config["reflection"]["enabled"] = False
    await scheduler.run_once("reflection")
    assert scheduler.status()["jobs"]["reflection"]["status"] == "checked"


@pytest.mark.asyncio
async def test_cancel_closes_clients_and_restart_has_only_one_task(care_config, monkeypatch):
    entered = asyncio.Event()
    client = SimpleNamespace(close=AsyncMock())

    class SlowEngine:
        check_interval_minutes = 5
        def __init__(self, config):
            # SDK clients can be shared by multiple model roles.
            self.client = self.daily_chat_memory_client = client
        async def run_due(self, *args):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setitem(care_scheduler.ENGINE_TYPES, "portrait", SlowEngine)
    care_config["portrait"]["enabled"] = True
    care_config["reflection"]["enabled"] = False
    scheduler = CareScheduler(make_service(care_config), initial_delay=0)
    await scheduler.start()
    first_task = scheduler._task
    await scheduler.start()
    assert scheduler._task is first_task
    await asyncio.wait_for(entered.wait(), 2)
    await scheduler.stop()
    assert first_task.done()
    assert not scheduler.status()["running"]
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_gateway_lifespan_starts_scheduler_and_always_closes_service():
    scheduler = SimpleNamespace(start=AsyncMock())
    service = SimpleNamespace(
        care_scheduler=scheduler, warm_recall_runtime=AsyncMock(), close=AsyncMock(),
    )
    app = gateway.create_gateway_app(config={"test": True}, service=service)
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            scheduler.start.assert_awaited_once()
            raise RuntimeError("request failure")
    service.close.assert_awaited_once()


def test_zeabur_assigns_scheduler_to_gateway_only(monkeypatch):
    from entrypoint_zeabur import build_child_env
    monkeypatch.delenv("OMBRE_CARE_SCHEDULER", raising=False)
    assert build_child_env("gateway")["OMBRE_CARE_SCHEDULER"] == "1"
    for role in ("brain", "proxy", "xinchao"):
        assert "OMBRE_CARE_SCHEDULER" not in build_child_env(role)
    monkeypatch.setenv("OMBRE_CARE_SCHEDULER", "0")
    assert build_child_env("gateway")["OMBRE_CARE_SCHEDULER"] == "0"


@pytest.mark.asyncio
async def test_activity_summary_is_stored_once_in_portrait(care_config, monkeypatch):
    care_config["portrait"]["enabled"] = True
    care_config["reflection"]["daily_activity_summary_enabled"] = True
    service = make_service(care_config)
    monkeypatch.setattr(care_scheduler.ReflectionEngine, "run_due", AsyncMock(return_value=[]))
    frozen = datetime(2026, 9, 11, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(care_scheduler.ReflectionEngine, "_local_now", lambda self: frozen)
    activity = AsyncMock(return_value={
        "status": "ready", "date": "2026-09-10",
        "activity_summary": {
            "timeline_id": "daily_activity_summary:2026-09-10", "source": "daily_activity_summary",
            "source_date": "2026-09-10", "text": "We completed the observatory together.",
            "confidence": 0.9,
        },
    })
    monkeypatch.setattr(care_scheduler.ReflectionEngine, "run_daily_activity_summary", activity)
    scheduler = CareScheduler(service)
    await scheduler.run_once("reflection")
    assert scheduler.status()["jobs"]["reflection"]["results"][0]["status"] == "stored"
    await scheduler.run_once("reflection")
    activity.assert_awaited_once()


@pytest.mark.asyncio
async def test_dream_outside_window_reports_skip(care_config, monkeypatch):
    care_config["dream"].update(enabled=True, auto_enabled=True, daily_hour=3)
    monkeypatch.setattr(care_scheduler.DreamEngine, "_now", lambda self, now=None: datetime(
        2026, 9, 11, 12, tzinfo=ZoneInfo("Asia/Shanghai")
    ))
    generate = AsyncMock()
    monkeypatch.setattr(care_scheduler.DreamEngine, "generate", generate)
    scheduler = CareScheduler(make_service(care_config))
    await scheduler.run_once("dream")
    assert scheduler.status()["jobs"]["dream"]["results"] == [
        {"status": "skipped", "reason": "outside_dream_window"},
    ]
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_request_recalls_memory_and_records_round_in_isolated_store(care_config, monkeypatch):
    monkeypatch.setenv("OMBRE_GATEWAY_TOKEN", "test-gateway-token")
    care_config["gateway"] = {
        "memory_sentinel_enabled": False, "domain_sentinel_enabled": False,
        "semantic_rescue_enabled": False, "recent_context_budget": 0,
        "upstreams": [{
            "name": "test", "base_url": "https://upstream.invalid/v1",
            "api_key": "test-only", "default_model": "test-model",
        }],
    }
    forwarded = []

    async def upstream(request):
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "test-reply", "model": "test-model",
            "choices": [{"message": {"role": "assistant", "content": "The access code is VIOLET-731."}, "finish_reason": "stop"}],
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    service = gateway.GatewayService(care_config, http_client=client)
    await service.bucket_mgr.create(
        bucket_id="observatory_access", name="Observatory access code",
        content="The observatory access code is VIOLET-731.",
        tags=["observatory", "access"], importance=8,
    )
    app = gateway.create_gateway_app(config=care_config, service=service)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as browser:
            response = await browser.post("/v1/chat/completions", json={
                "model": "test-model", "messages": [{"role": "user", "content": "What was the observatory access code?"}],
            }, headers={"X-Ombre-Session-Id": "isolated-care-check", "Authorization": "Bearer test-gateway-token"})
            assert response.status_code == 200, response.text
    assert len(forwarded) == 1
    assert "VIOLET-731" in json.dumps(forwarded[0]["messages"])
    now = datetime.now(timezone.utc)
    turns = service.state_store.list_conversation_turns_between(
        profile_id=service.persona_engine.profile_id,
        start_at=now - timedelta(minutes=5), end_at=now + timedelta(minutes=5),
    )
    assert len(turns) == 1
    assert turns[0]["session_id"] == "isolated-care-check"
    assert "VIOLET-731" in turns[0]["assistant_text"]
