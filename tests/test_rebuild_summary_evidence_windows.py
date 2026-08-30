from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.prepare_summary_review_queue import _load_raw_events
from scripts.rebuild_summary_evidence_windows import (
    build_evidence_windows,
    group_batches,
    load_shadow_ids,
    main,
)


def _make_db(path: Path, rows: list[tuple[str, str, str, str, str]]) -> str:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            create table raw_events (
                id integer primary key autoincrement,
                source text,
                source_event_id text,
                role text,
                text text,
                created_at text,
                conversation_id text,
                metadata_json text
            )
            """
        )
        conn.executemany(
            "insert into raw_events (source, source_event_id, role, text, created_at, conversation_id, metadata_json)"
            " values ('supabase', ?, ?, ?, ?, ?, '{\"assistant_id\": \"a-1\"}')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


def _artifact_item(summary_id: str, created_at: str, content: str) -> dict:
    return {
        "legacy_summary_id": summary_id,
        "created_at": created_at,
        "legacy_content": content,
        "assistant_id": "a-1",
    }


def test_candidates_never_postdate_the_summary_write(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path / "raw.sqlite",
        [
            ("before", "assistant", "严槿配置了向量模型和记忆库", "2026-07-24T14:00:00.000+00:00", "conv-a"),
            ("after", "assistant", "严槿配置了向量模型和记忆库", "2026-07-24T16:00:00.000+00:00", "conv-a"),
        ],
    )
    items = [_artifact_item("sum-1", "2026-07-24T15:00:00.000+00:00", "严槿配置了向量模型和记忆库")]

    payload = build_evidence_windows(items, _load_raw_events(db))

    ids = [c["source_event_id"] for c in payload["items"][0]["evidence_candidates"]]
    assert ids == ["before"]
    assert payload["ok"] is True


def test_timezone_shadow_events_are_dropped_from_candidates(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path / "raw.sqlite",
        [
            ("utc-1", "assistant", "严槿配置了向量模型和记忆库", "2026-07-24T06:00:00.000+00:00", "conv-a"),
            ("local-1", "assistant", "严槿配置了向量模型和记忆库", "2026-07-24T14:00:00+00:00", "conv-a"),
        ],
    )
    items = [_artifact_item("sum-1", "2026-07-24T15:00:00.000+00:00", "严槿配置了向量模型和记忆库")]

    payload = build_evidence_windows(items, _load_raw_events(db), shadow_ids={"local-1"})

    ids = [c["source_event_id"] for c in payload["items"][0]["evidence_candidates"]]
    assert ids == ["utc-1"]
    assert payload["stats"]["dropped_shadow_events"] == 1
    assert payload["stats"]["canonical_events"] == 1


def test_second_batch_only_reaches_back_to_the_previous_cron_write(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path / "raw.sqlite",
        [
            ("old", "assistant", "严槿配置了向量模型和记忆库", "2026-07-24T05:00:00.000+00:00", "conv-a"),
            ("recent", "assistant", "严槿配置了向量模型和记忆库", "2026-07-24T13:00:00.000+00:00", "conv-a"),
        ],
    )
    items = [
        _artifact_item("sum-1", "2026-07-24T06:00:00.000+00:00", "严槿配置了向量模型和记忆库"),
        _artifact_item("sum-2", "2026-07-24T15:00:00.000+00:00", "严槿配置了向量模型和记忆库"),
    ]

    payload = build_evidence_windows(items, _load_raw_events(db))
    by_id = {row["legacy_summary_id"]: row for row in payload["items"]}

    assert by_id["sum-1"]["window_kind"] == "initial_backfill"
    assert [c["source_event_id"] for c in by_id["sum-1"]["evidence_candidates"]] == ["old"]

    assert by_id["sum-2"]["window_kind"] == "cron_interval"
    assert by_id["sum-2"]["window_start"] == "2026-07-24T06:00:00+00:00"
    assert [c["source_event_id"] for c in by_id["sum-2"]["evidence_candidates"]] == ["recent"]


def test_long_cron_outage_clamps_the_lookback_window(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path / "raw.sqlite",
        [("stale", "assistant", "严槿配置了向量模型和记忆库", "2026-07-24T06:00:00.000+00:00", "conv-a")],
    )
    items = [
        _artifact_item("sum-1", "2026-07-24T05:00:00.000+00:00", "严槿配置了向量模型和记忆库"),
        _artifact_item("sum-2", "2026-07-26T05:00:00.000+00:00", "严槿配置了向量模型和记忆库"),
    ]

    payload = build_evidence_windows(items, _load_raw_events(db), max_lookback_hours=12)
    by_id = {row["legacy_summary_id"]: row for row in payload["items"]}

    assert by_id["sum-2"]["window_kind"] == "clamped_gap"
    assert by_id["sum-2"]["window_start"] == "2026-07-25T17:00:00+00:00"
    assert by_id["sum-2"]["evidence_candidates"] == []
    # sum-1 also finds nothing: the only raw event postdates it.
    assert by_id["sum-1"]["evidence_candidates"] == []
    assert payload["stats"]["no_candidate_summaries"] == 2


def test_summaries_written_together_share_one_batch_window() -> None:
    items = [
        _artifact_item("sum-1", "2026-07-24T15:00:00.000+00:00", "a"),
        _artifact_item("sum-2", "2026-07-24T15:00:20.000+00:00", "b"),
        _artifact_item("sum-3", "2026-07-24T21:00:00.000+00:00", "c"),
    ]

    assert group_batches(items, gap_seconds=300) == [[0, 1], [2]]


def test_unparsable_summary_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError, match="unparsable created_at"):
        group_batches([_artifact_item("sum-1", "not-a-time", "a")])


def test_load_shadow_ids_rejects_foreign_schema(tmp_path: Path) -> None:
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"schema_version": "nope", "shadow_source_event_ids": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported duplicate map schema"):
        load_shadow_ids(str(path))

    assert load_shadow_ids(None) == set()


def test_cli_writes_artifact_atomically(tmp_path: Path) -> None:
    db = _make_db(
        tmp_path / "raw.sqlite",
        [("before", "assistant", "严槿配置了向量模型和记忆库", "2026-07-24T14:00:00.000+00:00", "conv-a")],
    )
    artifact = tmp_path / "artifact.json"
    items = [_artifact_item("sum-1", "2026-07-24T15:00:00.000+00:00", "严槿配置了向量模型和记忆库")]
    artifact.write_text(json.dumps({"items": items, "input_rows": 1}, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "nested" / "windows.json"

    assert main(["--artifact", str(artifact), "--raw-db", db, "--output", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "ombre-summary-evidence-window-v1"
    assert payload["items"][0]["evidence_candidates"][0]["source_event_id"] == "before"
    assert not list(output.parent.glob(".*tmp"))