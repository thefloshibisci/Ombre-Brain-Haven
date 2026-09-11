"""Lifecycle-owned Care jobs for the Gateway sidecar beside the src/ Brain."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from dream_engine import DreamEngine
from portrait_engine import DailyPortraitMaintainer
from reflection_engine import ReflectionEngine


logger = logging.getLogger("ombre_brain.care")
ENGINE_TYPES = {
    "reflection": ReflectionEngine,
    "portrait": DailyPortraitMaintainer,
    "dream": DreamEngine,
}


@asynccontextmanager
async def _engine(engine_type, config):
    engine = engine_type(config)
    try:
        yield engine
    finally:
        clients = [getattr(engine, name, None) for name in (
            "client", "daily_chat_memory_client", "dehydration_client",
        )]
        closed = set()
        for client in clients:
            if client is None or id(client) in closed:
                continue
            closed.add(id(client))
            try:
                await client.close()
            except Exception as exc:
                logger.warning("Care client close failed: %s", type(exc).__name__)


def _result_summary(result):
    # Engine results can contain full memories, candidates and local file paths.
    return {
        key: result[key]
        for key in ("status", "reason", "period", "date")
        if isinstance(result.get(key), (str, int, bool))
    }


class CareScheduler:
    def __init__(self, service, *, initial_delay=20.0):
        self.service = service
        self.initial_delay = initial_delay
        self._task = None
        self._jobs = {name: {"status": "waiting"} for name in ENGINE_TYPES}

    def status(self):
        return {
            "running": self._task is not None and not self._task.done(),
            "jobs": deepcopy(self._jobs),
        }

    async def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="ombre-care-scheduler")
            logger.info("Care scheduler started: reflection, portrait, dream")

    async def stop(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        await asyncio.sleep(self.initial_delay)
        due_at = dict.fromkeys(ENGINE_TYPES, 0.0)
        while True:
            # Serialize jobs: reflection and portrait both update portrait state.
            for name in ENGINE_TYPES:
                if time.monotonic() >= due_at[name]:
                    interval = await self.run_once(name)
                    due_at[name] = time.monotonic() + interval
            await asyncio.sleep(max(0.01, min(due_at.values()) - time.monotonic()))

    async def run_once(self, name):
        interval = 300
        try:
            config = deepcopy(self.service.config)
            cfg = config.get(name) or {}
            interval = max(5, int(cfg.get("check_interval_minutes", 60))) * 60
            if not cfg.get("enabled", True) or not cfg.get("auto_enabled", True):
                results = [{"status": "disabled"}]
            else:
                async with _engine(ENGINE_TYPES[name], config) as engine:
                    interval = engine.check_interval_minutes * 60
                    results = await self._run(name, engine, config)
            if isinstance(results, dict):
                results = [results]
            summary = [_result_summary(result) for result in results]
            self._jobs[name] = {
                "status": "checked",
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "results": summary or [{"status": "not_due"}],
            }
            logger.info("Care %s checked: %s", name, self._jobs[name]["results"])
        except Exception as exc:
            self._jobs[name] = {
                "status": "error",
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reason": type(exc).__name__,
            }
            logger.warning("Care %s failed: %s", name, type(exc).__name__)
        return interval

    async def _run(self, name, engine, config):
        service = self.service
        if name == "dream":
            return await engine.run_due(
                service.bucket_mgr,
                service.embedding_engine,
                raw_event_store=service.raw_event_store,
            )
        if name == "portrait":
            return await engine.run_due(service.bucket_mgr, service.persona_engine)

        results = await engine.run_due(
            service.bucket_mgr,
            service.persona_engine,
            service.embedding_engine,
            service.state_store,
            service.raw_event_store,
        )
        now = engine._local_now()
        if not engine.daily_activity_summary_enabled or now.hour < engine.daily_chat_memory_hour:
            return results
        date_key = (now - timedelta(days=1)).date().isoformat()
        async with _engine(DailyPortraitMaintainer, config) as portrait:
            if not portrait.enabled or portrait.has_recent_timeline_item(
                date_key=date_key,
                source="daily_activity_summary",
                timeline_id=f"daily_activity_summary:{date_key}",
            ):
                return results
            candidates = [item for result in results for item in result.get("candidates", [])]
            impressions = [
                result["daily_impression"] for result in results if result.get("daily_impression")
            ]
            if not impressions:
                bucket = await service.bucket_mgr.get(f"reflection_daily_{date_key}")
                if bucket:
                    impressions.append({
                        "id": bucket["id"], "content": bucket.get("content", ""), "date": date_key,
                    })
            activity = await engine.run_daily_activity_summary(
                conversation_turn_store=service.state_store,
                raw_event_store=service.raw_event_store,
                persona_engine=service.persona_engine,
                daily_chat_memory_candidates=candidates,
                daily_impressions=impressions,
                key=date_key,
            )
            if activity.get("status") == "ready" and activity.get("activity_summary"):
                stored = portrait.upsert_recent_timeline_item(activity["activity_summary"], date_key)
                activity = {
                    **activity,
                    "status": "stored" if stored.get("status") == "updated" else stored.get("status", "error"),
                    **({"reason": stored["reason"]} if stored.get("reason") else {}),
                }
            results.append(activity)
        return results
