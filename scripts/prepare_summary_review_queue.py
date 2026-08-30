from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ALLOWED_DECISIONS = {"keep", "rewrite", "merge", "reject"}
ALLOWED_CONFIDENCE = {"none", "low", "medium", "high"}
SCHEMA_VERSION = "ombre-summary-review-queue-v1"
DEFAULT_SOURCE = "supabase"
MAX_CANDIDATES = 12
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]{2}", re.I)
_STOP = {"我们", "他们", "你们", "然后", "表示", "询问", "讨论", "进行", "以及", "一个", "这个", "之后", "今天", "两人", "严槿", "陆沉", "南枳"}


def normalized_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def normalized_hash(value: Any) -> str:
    return hashlib.sha256(normalized_text(value).encode("utf-8")).hexdigest()


def parse_time(value: Any) -> datetime | None:
    text = normalized_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def tokens(value: str) -> set[str]:
    text = normalized_text(value).lower()
    found = set(_TOKEN_RE.findall(text))
    # Keep distinctive single CJK characters only when the source is very short;
    # bigrams avoid turning common Chinese glue words into evidence.
    if len(found) < 2 and _CJK_RE.search(text):
        found.update(text[index : index + 2] for index in range(max(0, len(text) - 1)))
    return {token for token in found if token not in _STOP and len(token.strip()) > 1}


def lexical_score(summary_tokens: set[str], raw_text: str) -> float:
    raw_tokens = tokens(raw_text)
    if not summary_tokens or not raw_tokens:
        return 0.0
    intersection = len(summary_tokens & raw_tokens)
    # Recall is more useful than Jaccard for a short summary against a full turn.
    return round(intersection / len(summary_tokens), 6)


def _load_raw_events(raw_db: str) -> list[dict[str, Any]]:
    path = Path(raw_db)
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError(f"raw database is missing or empty: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='raw_events'"
        ).fetchone()
        if not table:
            raise ValueError("raw database has no raw_events table")
        rows = conn.execute(
            """
            SELECT source_event_id, role, text, created_at, conversation_id, metadata_json
            FROM raw_events WHERE source = ? AND source_event_id <> ''
            ORDER BY created_at, id
            """,
            (DEFAULT_SOURCE,),
        ).fetchall()
    finally:
        conn.close()
    events: list[dict[str, Any]] = []
    for source_event_id, role, text, created_at, conversation_id, metadata_json in rows:
        event_time = parse_time(created_at)
        try:
            metadata = json.loads(metadata_json or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        events.append(
            {
                "source_event_id": str(source_event_id),
                "role": str(role or ""),
                "text": str(text or ""),
                "created_at": str(created_at or ""),
                "created_time": event_time,
                "conversation_id": str(conversation_id or ""),
                "assistant_id": str(metadata.get("assistant_id") or ""),
            }
        )
    return events


def _load_artifact(path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise ValueError("summary artifact must be an object with an items array")
    items = payload["items"]
    if len(items) != int(payload.get("input_rows") or len(items)):
        raise ValueError("summary artifact input_rows does not match items length")
    return payload, items


def _candidate_events(item: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary_time = parse_time(item.get("created_at"))
    if summary_time is None:
        return []
    summary_tokens = tokens(str(item.get("legacy_content") or ""))
    assistant_id = str(item.get("assistant_id") or "")
    candidates: list[dict[str, Any]] = []
    for event in events:
        event_time = event["created_time"]
        if event_time is None:
            continue
        delta = abs((event_time - summary_time).total_seconds())
        # Summary timestamps are often generated immediately after a turn, but
        # allow a bounded day window for summaries spanning several turns.
        if delta > 36 * 3600:
            continue
        lexical = lexical_score(summary_tokens, event["text"])
        assistant_match = bool(assistant_id and assistant_id == event["assistant_id"])
        if lexical == 0 and not assistant_match:
            continue
        candidates.append(
            {
                "source_event_id": event["source_event_id"],
                "created_at": event["created_at"],
                "role": event["role"],
                "conversation_id": event["conversation_id"],
                "assistant_match": assistant_match,
                "time_delta_seconds": int(delta),
                "lexical_overlap": lexical,
            }
        )
    candidates.sort(
        key=lambda row: (
            -float(row["lexical_overlap"]),
            -int(bool(row["assistant_match"])),
            int(row["time_delta_seconds"]),
            row["source_event_id"],
        )
    )
    return candidates[:MAX_CANDIDATES]


def build_queue(artifact: dict[str, Any], items: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("summary artifact item must be an object")
        summary_id = normalized_text(item.get("legacy_summary_id"))
        content = normalized_text(item.get("legacy_content"))
        created_at = normalized_text(item.get("created_at"))
        if not summary_id or not content or parse_time(created_at) is None:
            raise ValueError(f"invalid summary artifact item: {summary_id or '<missing>'}")
        candidates = _candidate_events(item, events)
        queue.append(
            {
                "schema_version": SCHEMA_VERSION,
                "legacy_summary_id": summary_id,
                "legacy_summary_hash": normalized_hash(content),
                "created_at": created_at,
                "review_status": "pending",
                "legacy_review_status": normalized_text(item.get("legacy_review_status")) or "<missing>",
                "original_content": content,
                "original_content_sha256": normalized_hash(content),
                "decision": None,
                "rewritten_content": None,
                "merge_target_id": None,
                "source_event_ids": [],
                "evidence_candidates": candidates,
                "evidence_confidence": "low" if candidates else "none",
                "reviewer": None,
                "reviewed_at": None,
                "validation": {"status": "pending", "errors": []},
            }
        )
    return queue


def _validate_queue_rows(rows: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    ids: set[str] = set()
    raw_ids = {event["source_event_id"] for event in events}
    counts = Counter()
    for index, row in enumerate(rows, start=1):
        row_errors: list[str] = []
        if not isinstance(row, dict):
            errors.append({"line": index, "errors": ["row_not_object"]})
            continue
        summary_id = normalized_text(row.get("legacy_summary_id"))
        if not summary_id:
            row_errors.append("missing_legacy_summary_id")
        elif summary_id in ids:
            row_errors.append("duplicate_legacy_summary_id")
        else:
            ids.add(summary_id)
        content = normalized_text(row.get("original_content"))
        expected_hash = normalized_text(row.get("legacy_summary_hash"))
        if not content:
            row_errors.append("empty_original_content")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            row_errors.append("invalid_legacy_summary_hash")
        elif expected_hash != normalized_hash(content):
            row_errors.append("legacy_summary_hash_mismatch")
        if parse_time(row.get("created_at")) is None:
            row_errors.append("invalid_created_at")
        decision = row.get("decision")
        if decision is not None and decision not in ALLOWED_DECISIONS:
            row_errors.append("invalid_decision")
        source_ids = row.get("source_event_ids")
        if not isinstance(source_ids, list):
            row_errors.append("source_event_ids_not_array")
            source_ids = []
        if len(source_ids) != len(set(map(str, source_ids))):
            row_errors.append("duplicate_source_event_id")
        for source_id in source_ids:
            if not normalized_text(source_id):
                row_errors.append("empty_source_event_id")
            elif str(source_id) not in raw_ids:
                row_errors.append("unknown_source_event_id")
        rewritten = row.get("rewritten_content")
        target = normalized_text(row.get("merge_target_id"))
        if decision == "rewrite" and not normalized_text(rewritten):
            row_errors.append("rewrite_requires_rewritten_content")
        if decision == "merge":
            if not target:
                row_errors.append("merge_requires_target")
            elif target == summary_id:
                row_errors.append("merge_target_self_loop")
            elif target not in ids and target not in {normalized_text(other.get("legacy_summary_id")) for other in rows if isinstance(other, dict)}:
                row_errors.append("merge_target_unknown")
        if decision != "rewrite" and rewritten not in (None, ""):
            row_errors.append("rewritten_content_only_allowed_for_rewrite")
        if decision != "merge" and target:
            row_errors.append("merge_target_only_allowed_for_merge")
        confidence = row.get("evidence_confidence")
        if confidence not in ALLOWED_CONFIDENCE:
            row_errors.append("invalid_evidence_confidence")
        reviewer = normalized_text(row.get("reviewer"))
        reviewed_at = row.get("reviewed_at")
        if decision is not None:
            if not reviewer:
                row_errors.append("reviewed_decision_requires_reviewer")
            if parse_time(reviewed_at) is None:
                row_errors.append("reviewed_decision_requires_reviewed_at")
            if decision in {"keep", "rewrite"} and not source_ids:
                row_errors.append("content_decision_requires_source_event_ids")
        validation = row.get("validation")
        if not isinstance(validation, dict):
            row_errors.append("validation_not_object")
        if row_errors:
            errors.append({"line": index, "legacy_summary_id": summary_id, "errors": sorted(set(row_errors))})
            counts.update(row_errors)
    return {
        "ok": not errors,
        "schema_version": SCHEMA_VERSION,
        "queue_rows": len(rows),
        "unique_legacy_summary_ids": len(ids),
        "raw_event_ids_available": len(raw_ids),
        "decision_counts": dict(Counter("unreviewed" if row.get("decision") is None else row.get("decision") for row in rows if isinstance(row, dict))),
        "evidence_binding_counts": dict(Counter("bound" if row.get("source_event_ids") else "unbound" for row in rows if isinstance(row, dict))),
        "candidate_counts": {
            "with_candidates": sum(bool(row.get("evidence_candidates")) for row in rows if isinstance(row, dict)),
            "without_candidates": sum(not row.get("evidence_candidates") for row in rows if isinstance(row, dict)),
        },
        "error_counts": dict(counts),
        "errors": errors[:100],
        "error_count": len(errors),
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}") from exc
        rows.append(row)
    return rows


def cmd_build(args: argparse.Namespace) -> int:
    output = Path(args.output)
    report = Path(args.report)
    if output.exists() or report.exists():
        print("refusing to overwrite existing review queue or report", file=sys.stderr)
        return 2
    artifact, items = _load_artifact(args.artifact)
    events = _load_raw_events(args.raw_db)
    queue = build_queue(artifact, items, events)
    validation = _validate_queue_rows(queue, events)
    if not validation["ok"]:
        raise ValueError(f"generated queue failed validation: {validation['errors'][:3]}")
    payload = {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "source_artifact": str(Path(args.artifact).resolve()),
        "source_artifact_sha256": hashlib.sha256(Path(args.artifact).read_bytes()).hexdigest(),
        "raw_db": str(Path(args.raw_db).resolve()),
        "raw_event_count": len(events),
        "queue_count": len(queue),
        "reviewed_count": 0,
        "bound_count": 0,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "validation": validation,
    }
    write_jsonl(output, queue)
    write_json(report, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.queue)
    events = _load_raw_events(args.raw_db)
    validation = _validate_queue_rows(rows, events)
    payload = {
        "ok": validation["ok"],
        "queue": str(Path(args.queue).resolve()),
        "raw_db": str(Path(args.raw_db).resolve()),
        "validated_at": datetime.now(timezone.utc).isoformat(),
        **validation,
    }
    write_json(Path(args.report), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if validation["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare and validate an isolated legacy summary review queue")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--artifact", required=True)
    build.add_argument("--raw-db", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--report", required=True)
    build.set_defaults(handler=cmd_build)
    validate = sub.add_parser("validate")
    validate.add_argument("--queue", required=True)
    validate.add_argument("--raw-db", required=True)
    validate.add_argument("--report", required=True)
    validate.set_defaults(handler=cmd_validate)
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"summary review queue failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
