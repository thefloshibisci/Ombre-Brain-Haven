from __future__ import annotations

import json
from pathlib import Path

import pytest

DUPLICATE_MAP = {
    "schema_version": "ombre-raw-duplicate-map-v1",
    "clusters": [
        {
            "canonical_source_event_id": "utc-1",
            "shadow_source_event_ids": ["local-1"],
        }
    ],
}


def _write_map(tmp_path: Path, payload: dict) -> str:
    path = tmp_path / "duplicate-map.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_load_duplicate_map_returns_shadow_to_canonical(tmp_path: Path) -> None:
    from scripts.review_summary_queue import load_duplicate_map

    assert load_duplicate_map(_write_map(tmp_path, DUPLICATE_MAP)) == {"local-1": "utc-1"}
    assert load_duplicate_map(None) == {}


def test_load_duplicate_map_rejects_foreign_schema(tmp_path: Path) -> None:
    from scripts.review_summary_queue import load_duplicate_map

    path = _write_map(tmp_path, {**DUPLICATE_MAP, "schema_version": "something-else"})
    with pytest.raises(ValueError, match="unsupported duplicate map schema"):
        load_duplicate_map(path)


def test_load_duplicate_map_rejects_id_that_is_both_canonical_and_shadow(tmp_path: Path) -> None:
    from scripts.review_summary_queue import load_duplicate_map

    payload = {
        "schema_version": "ombre-raw-duplicate-map-v1",
        "clusters": [
            {"canonical_source_event_id": "a", "shadow_source_event_ids": ["b"]},
            {"canonical_source_event_id": "c", "shadow_source_event_ids": ["a"]},
        ],
    }
    with pytest.raises(ValueError, match="both canonical and shadow"):
        load_duplicate_map(_write_map(tmp_path, payload))


def test_show_canonicalizes_and_collapses_shadow_candidates() -> None:
    from scripts.review_summary_queue import inspect_row

    rows = [
        {
            "legacy_summary_id": "sum-1",
            "decision": None,
            "review_status": "pending",
            "original_content": "严槿问记忆库",
            "source_event_ids": [],
            "evidence_candidates": [
                {"source_event_id": "local-1", "role": "user", "lexical_overlap": 0.5},
                {"source_event_id": "utc-1", "role": "user", "lexical_overlap": 0.5},
                {"source_event_id": "utc-2", "role": "assistant", "lexical_overlap": 0.3},
            ],
        }
    ]
    events = [
        {"source_event_id": "utc-1", "text": "你能调用自己的记忆库不"},
        {"source_event_id": "utc-2", "text": "可以的"},
    ]

    payload = inspect_row(rows, events, index=1, shadow_to_canonical={"local-1": "utc-1"})

    shown = payload["evidence_candidates"]
    assert [c["bindable_source_event_id"] for c in shown] == ["utc-1", "utc-2"]
    assert shown[0]["is_timezone_shadow"] is True
    assert shown[0]["text"] == "你能调用自己的记忆库不"
    assert payload["collapsed_shadow_candidates"] == [
        {"hint_source_event_id": "utc-1", "canonical_source_event_id": "utc-1"}
    ]


def test_set_refuses_to_bind_a_shadow_event_and_names_the_canonical_id() -> None:
    from scripts.review_summary_queue import apply_review

    rows = [
        {
            "legacy_summary_id": "sum-1",
            "decision": None,
            "review_status": "pending",
            "original_content": "严槿问记忆库",
            "original_content_sha256": "x",
            "rewritten_content": None,
            "merge_target_id": None,
            "source_event_ids": [],
            "evidence_confidence": "low",
            "reviewer": None,
            "reviewed_at": None,
            "evidence_candidates": [],
            "validation": {"status": "pending", "errors": []},
        }
    ]
    events = [
        {"source_event_id": "utc-1", "text": "你能调用自己的记忆库不"},
        {"source_event_id": "local-1", "text": "你能调用自己的记忆库不"},
    ]

    with pytest.raises(ValueError, match=r"timezone-shadow raw events.*local-1 -> utc-1"):
        apply_review(
            rows,
            events,
            summary_id="sum-1",
            decision="keep",
            reviewer="严槿",
            reviewed_at="2026-08-30T12:00:00+00:00",
            source_event_ids=["local-1"],
            evidence_confidence="medium",
            shadow_to_canonical={"local-1": "utc-1"},
        )

    assert rows[0]["decision"] is None
    assert rows[0]["source_event_ids"] == []


def test_status_counts_rows_bound_to_shadow_events() -> None:
    from scripts.review_summary_queue import queue_status

    rows = [
        {"decision": "keep", "source_event_ids": ["utc-1"]},
        {"decision": "keep", "source_event_ids": ["local-1"]},
        {"decision": None, "source_event_ids": []},
    ]

    status = queue_status(rows, {"local-1": "utc-1"})

    assert status["evidence_bound_rows"] == 2
    assert status["shadow_bound_rows"] == 1
    assert status["unreviewed_rows"] == 1