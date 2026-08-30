from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_raw_event_duplicates import build_duplicate_map, load_events, main


def _make_db(path: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            create table raw_events (
                id integer primary key autoincrement,
                source_event_id text,
                conversation_id text,
                role text,
                text text,
                created_at text,
                metadata_json text
            )
            """
        )
        conn.executemany(
            "insert into raw_events (source_event_id, conversation_id, role, text, created_at, metadata_json)"
            " values (?, ?, ?, ?, ?, '{}')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_shifted_copy_becomes_shadow_and_precise_copy_stays_canonical(tmp_path: Path) -> None:
    db = tmp_path / "raw.sqlite"
    _make_db(
        db,
        [
            ("utc-1", "conv-a", "user", "你能调用自己的记忆库不", "2026-07-23T18:44:58.049+00:00"),
            ("local-1", "conv-a", "user", "你能调用自己的记忆库不", "2026-07-24T02:44:58+00:00"),
        ],
    )
    payload = build_duplicate_map(load_events(str(db)))

    assert payload["ok"] is True
    assert payload["totals"] == {
        "raw_events": 2,
        "shadow_clusters": 1,
        "shadow_events": 1,
        "canonical_events": 1,
        "conversations": 1,
        "conversations_with_shadows": 1,
        "unresolved_repeat_groups": 0,
    }
    cluster = payload["clusters"][0]
    assert cluster["canonical_source_event_id"] == "utc-1"
    assert cluster["shadow_source_event_ids"] == ["local-1"]
    assert cluster["canonical_rule"] == "subsecond_precision"
    assert payload["shadow_source_event_ids"] == ["local-1"]


def test_equal_precision_pair_falls_back_to_earliest_timestamp(tmp_path: Path) -> None:
    db = tmp_path / "raw.sqlite"
    _make_db(
        db,
        [
            ("late", "conv-a", "assistant", "同一句话", "2026-07-24T02:00:00+00:00"),
            ("early", "conv-a", "assistant", "同一句话", "2026-07-23T18:00:00+00:00"),
        ],
    )
    cluster = build_duplicate_map(load_events(str(db)))["clusters"][0]

    assert cluster["canonical_source_event_id"] == "early"
    assert cluster["shadow_source_event_ids"] == ["late"]
    assert cluster["canonical_rule"] == "earliest_timestamp"


def test_genuine_repeat_is_reported_but_never_shadowed(tmp_path: Path) -> None:
    db = tmp_path / "raw.sqlite"
    _make_db(
        db,
        [
            ("a", "conv-a", "user", "在吗", "2026-07-23T18:00:00.100+00:00"),
            ("b", "conv-a", "user", "在吗", "2026-07-23T18:00:47.200+00:00"),
        ],
    )
    payload = build_duplicate_map(load_events(str(db)))

    assert payload["clusters"] == []
    assert payload["shadow_source_event_ids"] == []
    assert payload["totals"]["canonical_events"] == 2
    repeat = payload["unresolved_repeats"][0]
    assert repeat["source_event_ids"] == ["a", "b"]
    assert repeat["max_gap_seconds"] == 47


def test_same_text_in_another_conversation_or_role_is_not_paired(tmp_path: Path) -> None:
    db = tmp_path / "raw.sqlite"
    _make_db(
        db,
        [
            ("a", "conv-a", "user", "早安", "2026-07-23T18:00:00.100+00:00"),
            ("b", "conv-b", "user", "早安", "2026-07-24T02:00:00+00:00"),
            ("c", "conv-a", "assistant", "早安", "2026-07-24T02:00:00+00:00"),
        ],
    )
    payload = build_duplicate_map(load_events(str(db)))

    assert payload["clusters"] == []
    assert payload["totals"]["canonical_events"] == 3


def test_unparsable_timestamp_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "raw.sqlite"
    _make_db(db, [("a", "conv-a", "user", "早安", "not-a-timestamp")])

    with pytest.raises(ValueError, match="unparsable created_at"):
        load_events(str(db))


def test_cli_writes_map_atomically(tmp_path: Path) -> None:
    db = tmp_path / "raw.sqlite"
    _make_db(
        db,
        [
            ("utc-1", "conv-a", "user", "记忆库", "2026-07-23T18:44:58.049+00:00"),
            ("local-1", "conv-a", "user", "记忆库", "2026-07-24T02:44:58+00:00"),
        ],
    )
    output = tmp_path / "nested" / "duplicate-map.json"

    assert main(["--raw-db", str(db), "--output", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "ombre-raw-duplicate-map-v1"
    assert payload["totals"]["shadow_events"] == 1
    assert not list(output.parent.glob(".*tmp"))