from __future__ import annotations

from datetime import datetime

from scripts.prepare_summary_review_queue import (
    SCHEMA_VERSION,
    _validate_queue_rows,
    build_queue,
)


def _event(event_id: str = "raw-1") -> dict:
    return {
        "source_event_id": event_id,
        "role": "user",
        "text": "严槿喜欢蓝色，今天讨论记忆库。",
        "created_at": "2026-08-01T00:00:01+00:00",
        "created_time": datetime.fromisoformat("2026-08-01T00:00:01+00:00"),
        "conversation_id": "conv-1",
        "assistant_id": "assistant-1",
    }


def _item(summary_id: str = "summary-1") -> dict:
    return {
        "legacy_summary_id": summary_id,
        "legacy_content": "严槿喜欢蓝色，讨论记忆库。",
        "created_at": "2026-08-01T00:00:00+00:00",
        "assistant_id": "assistant-1",
        "legacy_review_status": "candidate",
    }


def test_build_queue_is_unreviewed_and_candidates_are_metadata_only():
    rows = build_queue({"input_rows": 1}, [_item()], [_event()])
    row = rows[0]
    assert row["schema_version"] == SCHEMA_VERSION
    assert row["decision"] is None
    assert row["source_event_ids"] == []
    assert row["original_content_sha256"] == row["legacy_summary_hash"]
    assert row["evidence_candidates"][0]["source_event_id"] == "raw-1"
    assert "text" not in row["evidence_candidates"][0]


def test_validator_rejects_invalid_decision_missing_id_and_unknown_raw_event():
    rows = build_queue({}, [_item()], [_event()])
    rows[0]["decision"] = "discard"
    rows[0]["source_event_ids"] = ["missing-raw"]
    rows.append(dict(rows[0]))
    rows[1]["legacy_summary_id"] = ""
    report = _validate_queue_rows(rows, [_event()])
    assert report["ok"] is False
    assert "invalid_decision" in report["error_counts"]
    assert "unknown_source_event_id" in report["error_counts"]
    assert "missing_legacy_summary_id" in report["error_counts"]


def test_validator_rejects_merge_self_loop_and_bad_rewrite_contract():
    rows = build_queue({}, [_item()], [_event()])
    rows[0]["decision"] = "merge"
    rows[0]["merge_target_id"] = rows[0]["legacy_summary_id"]
    report = _validate_queue_rows(rows, [_event()])
    assert "merge_target_self_loop" in report["error_counts"]

    rows[0]["decision"] = "rewrite"
    rows[0]["merge_target_id"] = None
    rows[0]["rewritten_content"] = ""
    report = _validate_queue_rows(rows, [_event()])
    assert "rewrite_requires_rewritten_content" in report["error_counts"]


def test_validator_accepts_reviewed_keep_with_bound_evidence():
    rows = build_queue({}, [_item()], [_event()])
    rows[0]["decision"] = "keep"
    rows[0]["source_event_ids"] = ["raw-1"]
    rows[0]["evidence_confidence"] = "high"
    rows[0]["reviewer"] = "human-review"
    rows[0]["reviewed_at"] = "2026-08-30T12:00:00+00:00"
    report = _validate_queue_rows(rows, [_event()])
    assert report["ok"] is True
    assert report["decision_counts"] == {"keep": 1}
    assert report["evidence_binding_counts"] == {"bound": 1}


def test_validator_requires_audited_review_and_evidence_for_content_decisions():
    rows = build_queue({}, [_item()], [_event()])
    rows[0]["decision"] = "keep"
    report = _validate_queue_rows(rows, [_event()])
    assert "reviewed_decision_requires_reviewer" in report["error_counts"]
    assert "reviewed_decision_requires_reviewed_at" in report["error_counts"]
    assert "content_decision_requires_source_event_ids" in report["error_counts"]
