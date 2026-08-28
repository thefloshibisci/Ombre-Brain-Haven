from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_SOURCE = "supabase"
DEFAULT_BATCH = "supabase-v2-20260826"
ALLOWED_ROLES = {"user", "assistant"}
MAX_BATCH_SIZE = 1000
CSV_FIELD_LIMIT = 16 * 1024 * 1024
REPORT_MAX_CHARS = 320
REPO_ROOT = Path(__file__).resolve().parents[1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="Read-only CSV inventory")
    inventory.add_argument("--chat-csv", required=True)
    inventory.add_argument("--summary-csv", required=True)
    inventory.add_argument("--output-dir", required=True)

    sample = subparsers.add_parser("sample", help="Build a bounded dry-run sample manifest")
    sample.add_argument("--chat-csv", required=True)
    sample.add_argument("--summary-csv", required=True)
    sample.add_argument("--limit-raw", type=int, default=50)
    sample.add_argument("--limit-summaries", type=int, default=20)
    sample.add_argument("--raw-db", required=True)
    sample.add_argument("--report", required=True)

    summaries = subparsers.add_parser(
        "summaries", help="Build a bounded summary audit artifact"
    )
    summaries.add_argument("--summary-csv", required=True)
    summaries.add_argument("--report", required=True)

    apply_raw = subparsers.add_parser("apply-raw", help="Apply raw events to a staging DB")
    apply_raw.add_argument("--chat-csv", required=True)
    apply_raw.add_argument("--raw-db", required=True)
    apply_raw.add_argument("--batch-size", type=int, default=1000)
    apply_raw.add_argument("--report", required=True)
    apply_raw.add_argument("--apply", action="store_true")

    verify_raw = subparsers.add_parser("verify-raw", help="Verify a raw staging DB")
    verify_raw.add_argument("--chat-csv", required=True)
    verify_raw.add_argument("--raw-db", required=True)
    verify_raw.add_argument("--report", required=True)

    return parser.parse_args(argv)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_container_csv(path: str) -> tuple[list[dict[str, Any]], list[str]]:
    csv.field_size_limit(CSV_FIELD_LIMIT)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["export_date", "record_count", "records"]:
            raise ValueError(f"unexpected container columns: {reader.fieldnames}")
        for line_number, row in enumerate(reader, start=2):
            export_date = str(row.get("export_date") or "")
            try:
                declared = int(row.get("record_count") or 0)
            except ValueError as exc:
                raise ValueError(f"invalid record_count at line {line_number}") from exc
            try:
                records = json.loads(row.get("records") or "[]")
            except json.JSONDecodeError as exc:
                warnings.append(f"malformed_records_json:{export_date}:{line_number}")
                continue
            if not isinstance(records, list):
                warnings.append(f"records_not_array:{export_date}:{line_number}")
                continue
            if declared != len(records):
                warnings.append(
                    f"record_count_mismatch:{export_date}:{declared}:{len(records)}"
                )
            valid_rows = []
            for record in records:
                if not isinstance(record, dict):
                    warnings.append(f"malformed_record:{export_date}")
                    continue
                record_id = str(record.get("id") or "")
                if not record_id:
                    warnings.append(f"missing_id:{export_date}")
                    valid_rows.append(record)
                    continue
                if record_id in seen_ids:
                    duplicate_ids.add(record_id)
                    continue
                record["_export_date"] = export_date
                record["_line_number"] = line_number
                seen_ids.add(record_id)
                valid_rows.append(record)
            rows.extend(valid_rows)
    if duplicate_ids:
        warnings.append(f"duplicate_ids_ignored:{len(duplicate_ids)}")
    return rows, warnings


def normalized_hash(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_time(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return normalized


def summarize(
    rows: list[dict[str, Any]], kind: str, warnings: list[str] | None = None
) -> dict[str, Any]:
    ids = [str(row.get("id") or "") for row in rows]
    hashes = [normalized_hash(row.get("content")) for row in rows]
    times = [safe_time(row.get("created_at")) for row in rows]
    valid_times = [item for item in times if item]
    conversations = {
        str(row.get("conversation_id") or "") for row in rows if kind == "chat"
    }
    roles: dict[str, int] = {}
    empty_text = 0
    invalid_time = sum(item is None for item in times)
    missing_conversation = 0
    if kind == "chat":
        for row in rows:
            role = str(row.get("role") or "").strip().lower()
            roles[role or "<missing>"] = roles.get(role or "<missing>", 0) + 1
            if not str(row.get("content") or "").strip():
                empty_text += 1
            if not str(row.get("conversation_id") or "").strip():
                missing_conversation += 1
    elif kind == "summary":
        reviews: dict[str, int] = {}
        for row in rows:
            status = str(row.get("review_status") or "").strip().lower()
            reviews[status or "<missing>"] = reviews.get(status or "<missing>", 0) + 1
            if not str(row.get("content") or "").strip():
                empty_text += 1
        roles = reviews
    stats = {
        "input_rows": len(rows),
        "unique_ids": len(set(ids)),
        "duplicate_id_rows": len(ids) - len(set(ids)),
        "content_hash_groups": len(set(hashes)),
        "duplicate_content_rows": len(hashes) - len(set(hashes)),
        "empty_text": empty_text,
        "invalid_created_at": invalid_time,
        "time_min": min(valid_times) if valid_times else None,
        "time_max": max(valid_times) if valid_times else None,
        "conversation_count": len(conversations - {""}) if kind == "chat" else None,
        "missing_conversation": missing_conversation if kind == "chat" else None,
        "roles_or_review_status": roles,
    }
    stats["record_count_mismatch"] = sum(
        str(warning).startswith("record_count_mismatch") for warning in warnings or []
    )
    return stats


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def truncate(value: str) -> str:
    text = value.replace("\n", " ").strip()
    return text[:REPORT_MAX_CHARS] + ("..." if len(text) > REPORT_MAX_CHARS else "")


def load_chat_manifest(chat_csv: str) -> dict[str, Any]:
    source_path = Path(chat_csv)
    rows, warnings = read_container_csv(chat_csv)
    valid = 0
    rejected: dict[str, int] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    manifest_items: list[dict[str, Any]] = []
    for row in rows:
        reasons: list[str] = []
        source_id = str(row.get("id") or "").strip()
        role = str(row.get("role") or "").strip().lower()
        text = str(row.get("content") or "")
        created_at = safe_time(row.get("created_at"))
        if not source_id:
            reasons.append("missing_id")
        if role not in ALLOWED_ROLES:
            reasons.append("invalid_role")
        if not text.strip():
            reasons.append("empty_text")
        if created_at is None:
            reasons.append("invalid_created_at")
        if reasons:
            for reason in reasons:
                reject(reason)
            continue
        valid += 1
        export_date = str(row.get("_export_date") or "")
        manifest_items.append(
            {
                "source": DEFAULT_SOURCE,
                "source_event_id": source_id,
                "role": role,
                "text": text,
                "created_at": created_at,
                "conversation_id": str(row.get("conversation_id") or "").strip(),
                "session_id": "",
                "client": DEFAULT_SOURCE,
                "metadata": {
                    "assistant_id": str(row.get("assistant_id") or ""),
                    "export_date": export_date,
                    "migration_batch": DEFAULT_BATCH,
                    "source_file": source_path.name,
                    "source_table": "chat_messages",
                },
            }
        )
    return {
        "source_csv": source_path.name,
        "input_sha256": sha256_file(chat_csv),
        "container_warnings": warnings,
        "input_rows": len(rows),
        "valid_source_rows": valid,
        "rejected_rows": sum(rejected.values()),
        "rejection_reasons": rejected,
        "events": manifest_items,
    }


def store_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_staging_db(path: str) -> tuple[sqlite3.Connection, bool]:
    conn = store_connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS migration_checkpoint (
            input_sha256 TEXT PRIMARY KEY,
            input_rows INTEGER NOT NULL,
            valid_source_rows INTEGER NOT NULL,
            inserted_rows INTEGER NOT NULL,
            duplicate_rows INTEGER NOT NULL,
            rejected_rows INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS migration_batches (
            batch_index INTEGER PRIMARY KEY,
            input_sha256 TEXT NOT NULL,
            inserted_rows INTEGER NOT NULL,
            duplicate_rows INTEGER NOT NULL,
            rejected_rows INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    has_events = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_events')"
    ).fetchone()[0]
    return conn, bool(has_events)


def apply_raw_command(args: argparse.Namespace) -> int:
    if not args.apply:
        print("apply-raw requires --apply", file=sys.stderr)
        return 2
    if args.batch_size < 1 or args.batch_size > MAX_BATCH_SIZE:
        print(f"batch-size must be between 1 and {MAX_BATCH_SIZE}", file=sys.stderr)
        return 2
    manifest = load_chat_manifest(args.chat_csv)
    input_sha = manifest["input_sha256"]
    db_path = Path(args.raw_db)
    if db_path.exists() and db_path.stat().st_size:
        conn, has_events = initialize_staging_db(str(db_path))
        if not has_events:
            print("raw_events table missing from declared staging DB", file=sys.stderr)
            conn.close()
            return 2
        existing = conn.execute(
            "SELECT * FROM migration_checkpoint WHERE input_sha256 = ?", [input_sha]
        ).fetchone()
        if existing:
            print(
                "checkpoint exists; rerun is a no-op. Rerun verify-raw for final verification."
            )
            write_json(
                Path(args.report),
                {
                    "ok": True,
                    "rerun": True,
                    "source_file": Path(args.chat_csv).name,
                    "input_sha256": input_sha,
                    "input_rows": int(existing["input_rows"]),
                    "valid_source_rows": int(existing["valid_source_rows"]),
                    "inserted_rows": int(existing["inserted_rows"]),
                    "duplicate_rows": int(existing["duplicate_rows"]),
                    "rejected_rows": int(existing["rejected_rows"]),
                    "completion_equation_ok": int(existing["valid_source_rows"])
                    == int(existing["inserted_rows"]) + int(existing["duplicate_rows"]),
                    "db_path": str(db_path),
                },
            )
            conn.close()
            return 0
    else:
        conn, has_events = initialize_staging_db(str(db_path))
        if not has_events:
            sys.path.insert(0, str(REPO_ROOT))
            try:
                from raw_events import RawEventStore

                RawEventStore({"raw_events": {"db_path": str(db_path)}})
            except ImportError:
                print("Haven raw_events.py not importable from repository root", file=sys.stderr)
                conn.close()
                return 2

    sys.path.insert(0, str(REPO_ROOT))
    from raw_events import RawEventStore

    store = RawEventStore({"raw_events": {"db_path": str(db_path)}})
    batches = manifest["events"]
    total_inserted = total_duplicate = total_rejected = 0
    rejection_counts: dict[str, int] = {}
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for batch_index, start in enumerate(range(0, len(batches), args.batch_size)):
        chunk = batches[start : start + args.batch_size]
        result = store.ingest(chunk, source=DEFAULT_SOURCE)
        total_inserted += int(result["inserted"])
        total_duplicate += int(result["duplicate"])
        total_rejected += int(result["rejected"])
        for item in result["items"]:
            if item["status"] == "rejected":
                reason = str(item.get("reason") or "unknown")
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        batch_finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """
            INSERT OR REPLACE INTO migration_batches
            (batch_index, input_sha256, inserted_rows, duplicate_rows, rejected_rows, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                batch_index,
                input_sha,
                result["inserted"],
                result["duplicate"],
                result["rejected"],
                started_at,
                batch_finished,
            ],
        )
        conn.commit()
    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT OR REPLACE INTO migration_checkpoint
        (input_sha256, input_rows, valid_source_rows, inserted_rows, duplicate_rows, rejected_rows, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            input_sha,
            manifest["input_rows"],
            manifest["valid_source_rows"],
            total_inserted,
            total_duplicate,
            total_rejected,
            started_at,
            finished_at,
        ],
    )
    conn.commit()
    conn.close()
    report = {
        "ok": True,
        "source_file": Path(args.chat_csv).name,
        "input_sha256": input_sha,
        "input_rows": manifest["input_rows"],
        "valid_source_rows": manifest["valid_source_rows"],
        "inserted_rows": total_inserted,
        "duplicate_rows": total_duplicate,
        "rejected_rows": total_rejected,
        "rejection_reasons": rejection_counts,
        "completion_equation_ok": manifest["valid_source_rows"] == total_inserted + total_duplicate,
        "db_path": str(db_path),
    }
    write_json(Path(args.report), report)
    print(f"apply-raw complete: {report}")
    return 0


def verify_raw_command(args: argparse.Namespace) -> int:
    conn = store_connect(args.raw_db)
    try:
        checkpoint = conn.execute(
            "SELECT * FROM migration_checkpoint ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        batch_count = int(
            conn.execute("SELECT COUNT(*) FROM migration_batches").fetchone()[0]
        )
        source = conn.execute(
            "SELECT source, COUNT(*) AS count FROM raw_events GROUP BY source ORDER BY source"
        ).fetchall()
        role = conn.execute(
            "SELECT role, COUNT(*) AS count FROM raw_events GROUP BY role ORDER BY role"
        ).fetchall()
        time_range = conn.execute(
            "SELECT MIN(created_at) AS time_min, MAX(created_at) AS time_max FROM raw_events"
        ).fetchone()
        conversation = conn.execute(
            """
            SELECT COUNT(DISTINCT conversation_id) AS conversation_count,
                   SUM(CASE WHEN conversation_id = '' THEN 1 ELSE 0 END) AS missing_conversation
            FROM raw_events
            """
        ).fetchone()
        fts = conn.execute(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_events_fts')"
        ).fetchone()[0]
        manifest = load_chat_manifest(args.chat_csv)
        valid = manifest["valid_source_rows"]
        inserted = int(checkpoint["inserted_rows"]) if checkpoint else 0
        duplicate = int(checkpoint["duplicate_rows"]) if checkpoint else 0
        equation_ok = bool(checkpoint) and valid == inserted + duplicate
        sys.path.insert(0, str(REPO_ROOT))
        from raw_events import RawEventStore
        store = RawEventStore({"raw_events": {"db_path": args.raw_db}})
        date_probe = store.list_events_between(
            start_at=datetime.fromisoformat(str(time_range["time_min"] or "").replace("Z", "+00:00")),
            end_at=datetime.fromisoformat(str(time_range["time_max"] or "").replace("Z", "+00:00"))
            + timedelta(seconds=1),
            source=DEFAULT_SOURCE,
            limit=1,
        )
        quote_probe_text = str(
            conn.execute(
                "SELECT text FROM raw_events WHERE source = ? ORDER BY id LIMIT 1",
                [DEFAULT_SOURCE],
            ).fetchone()[0]
        )[:12]
        quote_probe = store.search(quote_probe_text, source=DEFAULT_SOURCE, limit=1)
        report = {
            "ok": bool(checkpoint) and equation_ok,
            "source_file": Path(args.chat_csv).name,
            "input_sha256": manifest["input_sha256"],
            "input_rows": manifest["input_rows"],
            "valid_source_rows": valid,
            "inserted_rows": inserted,
            "duplicate_rows": duplicate,
            "rejected_rows": int(checkpoint["rejected_rows"]) if checkpoint else 0,
            "completion_equation_ok": equation_ok,
            "checkpoint_present": bool(checkpoint),
            "batch_count": batch_count,
            "db_counts_by_source": {row["source"]: row["count"] for row in source},
            "db_counts_by_role": {row["role"]: row["count"] for row in role},
            "db_time_min": time_range["time_min"],
            "db_time_max": time_range["time_max"],
            "conversation_count": conversation["conversation_count"],
            "missing_conversation_rows": conversation["missing_conversation"],
            "fts_available": bool(fts),
            "date_probe_hit": len(date_probe) > 0,
            "quote_probe_hit": bool(quote_probe.get("count")),
        }
        write_json(Path(args.report), report)
        print(f"verify-raw complete: {report}")
        return 0 if report["ok"] else 1
    finally:
        conn.close()


def inventory_command(args: argparse.Namespace) -> int:
    chat_rows, chat_warnings = read_container_csv(args.chat_csv)
    summary_rows, summary_warnings = read_container_csv(args.summary_csv)
    payload = {
        "ok": True,
        "chat": {
            "source_file": Path(args.chat_csv).name,
            "input_sha256": sha256_file(args.chat_csv),
            "stats": summarize(chat_rows, "chat"),
            "container_warnings": chat_warnings,
        },
        "summary": {
            "source_file": Path(args.summary_csv).name,
            "input_sha256": sha256_file(args.summary_csv),
            "stats": summarize(summary_rows, "summary"),
            "container_warnings": summary_warnings,
        },
        "counts_match_contract": {
            "chat_11919": len(chat_rows) == 11919,
            "summary_842": len(summary_rows) == 842,
        },
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "inventory.json", payload)
    print(f"inventory complete: {output_dir / 'inventory.json'}")
    return 0


def summary_command(args: argparse.Namespace) -> int:
    report_path = Path(args.report)
    if report_path.exists():
        print("summary report already exists; refusing to overwrite", file=sys.stderr)
        return 2
    rows, warnings = read_container_csv(args.summary_csv)
    payload = {
        "ok": True,
        "source_file": Path(args.summary_csv).name,
        "input_sha256": sha256_file(args.summary_csv),
        "input_rows": len(rows),
        "container_warnings": warnings,
        "items": [
            {
                "legacy_summary_id": str(row.get("id") or ""),
                "legacy_content": str(row.get("content") or ""),
                "created_at": str(row.get("created_at") or ""),
                "reviewed_at": str(row.get("reviewed_at") or ""),
                "assistant_id": str(row.get("assistant_id") or ""),
                "legacy_review_status": str(row.get("review_status") or ""),
                "legacy_summary_hash": normalized_hash(row.get("content")),
                "source_event_ids": [],
                "rewrite_version": "supabase-summary-rewrite-v1",
                "decision": None,
                "evidence_confidence": "low",
            }
            for row in rows
        ],
    }
    write_json(report_path, payload)
    print(f"summary artifact complete: {args.report}")
    return 0


def sample_command(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(REPO_ROOT))
    chat_rows, chat_warnings = read_container_csv(args.chat_csv)
    summary_rows, summary_warnings = read_container_csv(args.summary_csv)
    raw_limit = max(0, int(args.limit_raw))
    summary_limit = max(0, int(args.limit_summaries))
    chat_manifest = load_chat_manifest(args.chat_csv)
    raw_sample = chat_manifest["events"][:raw_limit]
    db_path = Path(args.raw_db)
    if db_path.exists() and db_path.stat().st_size:
        print("sample raw-db must not already exist", file=sys.stderr)
        return 2
    conn, has_events = initialize_staging_db(str(db_path))
    if has_events:
        print("sample raw-db already has raw_events table", file=sys.stderr)
        conn.close()
        return 2
    from raw_events import RawEventStore

    store = RawEventStore({"raw_events": {"db_path": str(db_path)}})
    result = store.ingest(raw_sample, source=DEFAULT_SOURCE)
    conn.close()
    sample_report = {
        "ok": True,
        "source_file": Path(args.chat_csv).name,
        "input_sha256": chat_manifest["input_sha256"],
        "limit_raw": raw_limit,
        "limit_summaries": summary_limit,
        "input_rows": chat_manifest["input_rows"],
        "sample_raw_count": len(raw_sample),
        "inserted_rows": result["inserted"],
        "duplicate_rows": result["duplicate"],
        "rejected_rows": result["rejected"],
        "rejection_reasons": {},
        "completion_equation_ok": len(raw_sample)
        == result["inserted"] + result["duplicate"],
        "db_path": str(db_path),
        "container_warnings": chat_warnings + summary_warnings,
    }
    write_json(Path(args.report), sample_report)
    print(f"sample complete: {args.report}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    handlers: dict[str, Callable[[], int]] = {
        "inventory": lambda: inventory_command(args),
        "sample": lambda: sample_command(args),
        "apply-raw": lambda: apply_raw_command(args),
        "verify-raw": lambda: verify_raw_command(args),
        "summaries": lambda: summary_command(args),
    }
    return handlers[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
