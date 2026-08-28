from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.migrate_supabase_archive import (
    apply_raw_command,
    load_chat_manifest,
    main,
    read_container_csv,
    sample_command,
    summarize,
    summary_command,
    verify_raw_command,
)


def write_container(path: Path, rows: list[dict], *, bad_count: bool = False) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["export_date", "record_count", "records"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "export_date": "2026-08-01",
                "record_count": len(rows) + (1 if bad_count else 0),
                "records": json.dumps(rows, ensure_ascii=False),
            }
        )


def chat_rows() -> list[dict]:
    return [
        {
            "id": "raw-1",
            "role": "user",
            "content": "hello",
            "created_at": "2026-08-01T00:00:00+00:00",
            "assistant_id": "assistant-1",
            "conversation_id": "conversation-1",
        },
        {
            "id": "raw-2",
            "role": "assistant",
            "content": "hi",
            "created_at": "2026-08-01T00:00:01Z",
            "assistant_id": "assistant-1",
            "conversation_id": "conversation-1",
        },
    ]


def summary_rows() -> list[dict]:
    return [
        {
            "id": "summary-1",
            "content": "subject did a specific thing",
            "created_at": "2026-08-01T01:00:00+00:00",
            "reviewed_at": None,
            "assistant_id": "assistant-1",
            "review_status": "backlog",
        }
    ]


def test_container_reader_deduplicates_across_rows(tmp_path: Path) -> None:
    path = tmp_path / "chat.csv"
    rows = chat_rows()
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["export_date", "record_count", "records"]
        )
        writer.writeheader()
        writer.writerow(
            {
                "export_date": "2026-08-01",
                "record_count": 1,
                "records": json.dumps(rows[:1]),
            }
        )
        writer.writerow(
            {
                "export_date": "2026-08-02",
                "record_count": 2,
                "records": json.dumps([rows[0], rows[1]]),
            }
        )
    parsed, warnings = read_container_csv(str(path))

    assert len(parsed) == 2
    assert "duplicate_ids_ignored:1" in warnings


def test_chat_manifest_filters_invalid_records(tmp_path: Path) -> None:
    path = tmp_path / "chat.csv"
    rows = chat_rows()
    rows.append({"id": "", "role": "user", "content": "x"})
    rows.append({"id": "bad-role", "role": "tool", "content": "x"})
    rows.append({"id": "bad-time", "role": "user", "content": "x", "created_at": "nope"})
    write_container(path, rows)

    manifest = load_chat_manifest(str(path))

    assert manifest["input_rows"] == 5
    assert manifest["valid_source_rows"] == 2
    assert manifest["rejected_rows"] == 5
    assert manifest["rejection_reasons"]["missing_id"] == 1
    assert manifest["rejection_reasons"]["invalid_role"] == 1
    assert manifest["rejection_reasons"]["invalid_created_at"] == 3
    assert manifest["events"][0]["metadata"]["source_file"] == "chat.csv"
    assert manifest["events"][0]["metadata"]["export_date"] == "2026-08-01"


def test_summary_inventory_and_artifact(tmp_path: Path) -> None:
    source = tmp_path / "summary.csv"
    write_container(source, summary_rows())
    report = tmp_path / "summary.json"
    args = argparse.Namespace(
        summary_csv=str(source),
        report=str(report),
    )
    assert summary_command(args) == 0

    assert summarize(read_container_csv(str(source))[0], "summary")["input_rows"] == 1

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["items"][0]["legacy_summary_id"] == "summary-1"
    assert payload["items"][0]["source_event_ids"] == []
    assert payload["items"][0]["decision"] is None


def test_sample_and_verify_completion_equation(tmp_path: Path) -> None:
    source = tmp_path / "chat.csv"
    write_container(source, chat_rows())
    raw_db = tmp_path / "raw_events.sqlite"
    report = tmp_path / "sample.json"
    args = argparse.Namespace(
        chat_csv=str(source),
        summary_csv=str(source),
        limit_raw=50,
        limit_summaries=20,
        raw_db=str(raw_db),
        report=str(report),
    )

    assert sample_command(args) == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["input_rows"] == 2
    assert payload["sample_raw_count"] == 2
    assert payload["completion_equation_ok"] is True

    verify_report = tmp_path / "verify.json"
    verify_args = argparse.Namespace(
        chat_csv=str(source), raw_db=str(raw_db), report=str(verify_report)
    )
    assert verify_raw_command(verify_args) == 1
    verify_payload = json.loads(verify_report.read_text(encoding="utf-8"))
    assert verify_payload["checkpoint_present"] is False


def test_apply_raw_is_explicit_and_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = tmp_path / "chat.csv"
    write_container(source, chat_rows())
    raw_db = tmp_path / "raw_events.sqlite"
    report = tmp_path / "apply.json"

    dry_run_args = argparse.Namespace(
        chat_csv=str(source),
        raw_db=str(raw_db),
        batch_size=1,
        report=str(report),
        apply=False,
    )
    assert apply_raw_command(dry_run_args) == 2
    assert capsys.readouterr().err.strip() == "apply-raw requires --apply"

    apply_args = argparse.Namespace(
        chat_csv=str(source),
        raw_db=str(raw_db),
        batch_size=1,
        report=str(report),
        apply=True,
    )
    assert apply_raw_command(apply_args) == 0
    first = json.loads(report.read_text(encoding="utf-8"))
    assert first["completion_equation_ok"] is True
    assert first["inserted_rows"] == 2

    rerun_report = tmp_path / "rerun.json"
    apply_args.report = str(rerun_report)
    assert apply_raw_command(apply_args) == 0
    second = json.loads(rerun_report.read_text(encoding="utf-8"))
    assert second["inserted_rows"] == 2
    assert second["duplicate_rows"] == 0
    assert second["completion_equation_ok"] is True

    conn = sqlite3.connect(raw_db)
    assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM migration_checkpoint").fetchone()[0] == 1
    conn.close()
