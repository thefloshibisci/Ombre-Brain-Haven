from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from scripts.prepare_summary_review_queue import SCHEMA_VERSION, normalized_hash
from scripts.review_summary_queue import (
    apply_review,
    initialize_working_queue,
    inspect_row,
    queue_status,
)


def _event(event_id: str = "raw-1", text: str = "严槿喜欢蓝色，今天讨论记忆库。") -> dict:
    return {
        "source_event_id": event_id,
        "role": "user",
        "text": text,
        "created_at": "2026-08-01T00:00:01+00:00",
        "created_time": datetime.fromisoformat("2026-08-01T00:00:01+00:00"),
        "conversation_id": "conv-1",
        "assistant_id": "assistant-1",
    }


def _row(summary_id: str = "summary-1") -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "legacy_summary_id": summary_id,
        "legacy_summary_hash": normalized_hash("严槿喜欢蓝色，讨论记忆库。"),
        "created_at": "2026-08-01T00:00:00+00:00",
        "review_status": "pending",
        "legacy_review_status": "candidate",
        "original_content": "严槿喜欢蓝色，讨论记忆库。",
        "original_content_sha256": normalized_hash("严槿喜欢蓝色，讨论记忆库。"),
        "decision": None,
        "rewritten_content": None,
        "merge_target_id": None,
        "source_event_ids": [],
        "evidence_candidates": [
            {
                "source_event_id": "raw-1",
                "created_at": "2026-08-01T00:00:01+00:00",
                "role": "user",
                "conversation_id": "conv-1",
                "assistant_match": True,
                "time_delta_seconds": 1,
                "lexical_overlap": 1.0,
            }
        ],
        "evidence_confidence": "low",
        "reviewer": None,
        "reviewed_at": None,
        "validation": {"status": "pending", "errors": []},
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def test_initialize_working_queue_refuses_baseline_overwrite_and_existing_output(tmp_path: Path):
    source = tmp_path / "baseline.jsonl"
    output = tmp_path / "working.jsonl"
    _write_jsonl(source, [_row()])

    with pytest.raises(ValueError, match="must not overwrite"):
        initialize_working_queue(str(source), str(source))

    result = initialize_working_queue(str(source), str(output))
    assert result["queue_rows"] == 1
    assert output.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        initialize_working_queue(str(source), str(output))


def test_inspect_row_expands_candidate_metadata_with_raw_text():
    payload = inspect_row([_row()], [_event()], next_pending=True, max_chars=8)
    candidate = payload["evidence_candidates"][0]
    assert payload["queue_index"] == 1
    assert candidate["source_event_id"] == "raw-1"
    assert candidate["text"] == "严槿喜欢蓝色，今"
    assert candidate["text_truncated"] is True


def test_apply_review_records_valid_keep_and_status():
    rows = [_row()]
    result = apply_review(
        rows,
        [_event()],
        summary_id="summary-1",
        decision="keep",
        reviewer="yanjin",
        reviewed_at="2026-08-30T20:00:00+08:00",
        source_event_ids=["raw-1", "raw-1"],
        evidence_confidence="high",
    )
    assert result["validation"]["ok"] is True
    assert rows[0]["decision"] == "keep"
    assert rows[0]["source_event_ids"] == ["raw-1"]
    assert rows[0]["reviewed_at"] == "2026-08-30T12:00:00+00:00"
    assert queue_status(rows)["reviewed_rows"] == 1


def test_apply_review_rejects_unsupported_content_decision_and_bad_confidence():
    rows = [_row()]
    with pytest.raises(ValueError, match="requires at least one"):
        apply_review(
            rows,
            [_event()],
            summary_id="summary-1",
            decision="rewrite",
            reviewer="yanjin",
            reviewed_at="2026-08-30T20:00:00+08:00",
            rewritten_content="严槿喜欢蓝色。",
            evidence_confidence="none",
        )

    with pytest.raises(ValueError, match="non-none evidence confidence"):
        apply_review(
            rows,
            [_event()],
            summary_id="summary-1",
            decision="reject",
            reviewer="yanjin",
            reviewed_at="2026-08-30T20:00:00+08:00",
            evidence_confidence="high",
        )


def test_apply_review_supports_merge_and_reject_without_automatic_evidence_binding():
    rows = [_row("summary-1"), _row("summary-2")]
    merged = apply_review(
        rows,
        [_event()],
        summary_id="summary-1",
        decision="merge",
        reviewer="yanjin",
        reviewed_at="2026-08-30T20:00:00+08:00",
        merge_target_id="summary-2",
        evidence_confidence="none",
    )
    assert merged["validation"]["ok"] is True
    assert rows[0]["source_event_ids"] == []

    rejected = apply_review(
        rows,
        [_event()],
        summary_id="summary-2",
        decision="reject",
        reviewer="yanjin",
        reviewed_at="2026-08-30T20:01:00+08:00",
        evidence_confidence="none",
    )
    assert rejected["validation"]["ok"] is True
