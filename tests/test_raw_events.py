from __future__ import annotations

from pathlib import Path

from raw_events import RawEventStore


def _store(tmp_path: Path) -> RawEventStore:
    return RawEventStore({"state_dir": str(tmp_path), "raw_events": {"max_ingest_batch": 10}})


def test_future_raw_events_are_saved_verbatim_and_searchable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = "未来原文保存验收词：银杏桥\n保留换行"

    result = store.ingest(
        [
            {
                "source_event_id": "future-1",
                "role": "user",
                "text": original,
                "conversation_id": "conversation-1",
                "session_id": "session-1",
                "created_at": "2026-08-31T12:00:00+08:00",
            }
        ],
        source="future-test",
    )

    found = store.search("银杏桥", limit=10, source="future-test")

    assert result["ok"] is True
    assert result["inserted"] == 1
    assert found["count"] == 1
    assert found["items"][0]["text"] == original
    assert found["items"][0]["source_event_id"] == "future-1"


def test_future_raw_ingest_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = {
        "source_event_id": "future-duplicate",
        "role": "assistant",
        "text": "同一条未来原文不应重复保存",
        "conversation_id": "conversation-1",
        "session_id": "session-1",
    }

    first = store.ingest([event], source="future-test")
    second = store.ingest([event], source="future-test")

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["duplicate"] == 1
    assert store.search("未来原文", source="future-test")["count"] == 1


def test_raw_store_rejects_non_dialogue_and_injected_context(tmp_path: Path) -> None:
    store = _store(tmp_path)

    result = store.ingest(
        [
            {"source_event_id": "system-1", "role": "system", "text": "不要入档"},
            {
                "source_event_id": "injection-1",
                "role": "user",
                "text": "Core Memory:\n[bucket_id:secret]",
            },
        ],
        source="future-test",
    )

    assert result["inserted"] == 0
    assert result["rejected"] == 2
    assert {item["reason"] for item in result["items"]} == {"invalid_role", "injected_context"}
