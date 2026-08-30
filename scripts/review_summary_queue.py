from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scripts.prepare_summary_review_queue import (
        ALLOWED_CONFIDENCE,
        ALLOWED_DECISIONS,
        _load_raw_events,
        _validate_queue_rows,
        normalized_text,
        parse_time,
        read_jsonl,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from prepare_summary_review_queue import (
        ALLOWED_CONFIDENCE,
        ALLOWED_DECISIONS,
        _load_raw_events,
        _validate_queue_rows,
        normalized_text,
        parse_time,
        read_jsonl,
    )


def _find_row(rows: list[dict[str, Any]], summary_id: str) -> tuple[int, dict[str, Any]]:
    wanted = normalized_text(summary_id)
    matches = [(index, row) for index, row in enumerate(rows) if normalized_text(row.get("legacy_summary_id")) == wanted]
    if not matches:
        raise ValueError(f"unknown legacy_summary_id: {wanted or '<missing>'}")
    if len(matches) != 1:
        raise ValueError(f"duplicate legacy_summary_id in queue: {wanted}")
    return matches[0]


def _dedupe(values: list[str] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        item = normalized_text(value)
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _atomic_write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def initialize_working_queue(source: str, output: str) -> dict[str, Any]:
    source_path = Path(source).resolve()
    output_path = Path(output).resolve()
    if source_path == output_path:
        raise ValueError("working queue must not overwrite the generated baseline queue")
    if output_path.exists():
        raise ValueError(f"refusing to overwrite existing working queue: {output_path}")
    rows = read_jsonl(str(source_path))
    _atomic_write_jsonl(output_path, rows)
    return {"ok": True, "source": str(source_path), "output": str(output_path), "queue_rows": len(rows)}


def queue_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = Counter("unreviewed" if row.get("decision") is None else str(row.get("decision")) for row in rows)
    bound = sum(bool(row.get("source_event_ids")) for row in rows)
    return {
        "queue_rows": len(rows),
        "decision_counts": dict(decisions),
        "reviewed_rows": len(rows) - decisions.get("unreviewed", 0),
        "unreviewed_rows": decisions.get("unreviewed", 0),
        "evidence_bound_rows": bound,
    }


def select_row(rows: list[dict[str, Any]], summary_id: str | None, index: int | None, next_pending: bool) -> tuple[int, dict[str, Any]]:
    selectors = int(bool(summary_id)) + int(index is not None) + int(next_pending)
    if selectors != 1:
        raise ValueError("select exactly one of --id, --index, or --next-pending")
    if summary_id:
        return _find_row(rows, summary_id)
    if index is not None:
        if index < 1 or index > len(rows):
            raise ValueError(f"index must be between 1 and {len(rows)}")
        return index - 1, rows[index - 1]
    for row_index, row in enumerate(rows):
        if row.get("decision") is None:
            return row_index, row
    raise ValueError("queue has no pending rows")


def inspect_row(
    rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    summary_id: str | None = None,
    index: int | None = None,
    next_pending: bool = False,
    candidate_limit: int = 12,
    max_chars: int = 4000,
) -> dict[str, Any]:
    row_index, row = select_row(rows, summary_id, index, next_pending)
    event_by_id = {event["source_event_id"]: event for event in events}
    candidates: list[dict[str, Any]] = []
    for rank, candidate in enumerate(row.get("evidence_candidates") or [], start=1):
        if len(candidates) >= candidate_limit:
            break
        event = event_by_id.get(normalized_text(candidate.get("source_event_id")))
        if event is None:
            text = None
            text_truncated = False
        else:
            full_text = str(event.get("text") or "")
            text = full_text[:max_chars]
            text_truncated = len(full_text) > max_chars
        candidates.append(
            {
                "rank": rank,
                **candidate,
                "text": text,
                "text_truncated": text_truncated,
            }
        )
    return {
        "queue_index": row_index + 1,
        "queue_rows": len(rows),
        "legacy_summary_id": row.get("legacy_summary_id"),
        "created_at": row.get("created_at"),
        "decision": row.get("decision"),
        "review_status": row.get("review_status"),
        "original_content": row.get("original_content"),
        "rewritten_content": row.get("rewritten_content"),
        "merge_target_id": row.get("merge_target_id"),
        "source_event_ids": row.get("source_event_ids"),
        "evidence_confidence": row.get("evidence_confidence"),
        "reviewer": row.get("reviewer"),
        "reviewed_at": row.get("reviewed_at"),
        "evidence_candidates": candidates,
    }


def apply_review(
    rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    summary_id: str,
    decision: str,
    reviewer: str,
    reviewed_at: str,
    source_event_ids: list[str] | None = None,
    evidence_confidence: str = "none",
    rewritten_content: str | None = None,
    merge_target_id: str | None = None,
) -> dict[str, Any]:
    if decision not in ALLOWED_DECISIONS:
        raise ValueError(f"decision must be one of: {', '.join(sorted(ALLOWED_DECISIONS))}")
    reviewer = normalized_text(reviewer)
    if not reviewer:
        raise ValueError("reviewer is required")
    if parse_time(reviewed_at) is None:
        raise ValueError("reviewed_at must be an ISO-8601 timestamp")
    if evidence_confidence not in ALLOWED_CONFIDENCE:
        raise ValueError(f"evidence_confidence must be one of: {', '.join(sorted(ALLOWED_CONFIDENCE))}")

    row_index, row = _find_row(rows, summary_id)
    source_ids = _dedupe(source_event_ids)
    raw_ids = {event["source_event_id"] for event in events}
    missing = [source_id for source_id in source_ids if source_id not in raw_ids]
    if missing:
        raise ValueError(f"unknown source_event_id: {', '.join(missing)}")

    rewritten = normalized_text(rewritten_content)
    target = normalized_text(merge_target_id)
    if decision == "rewrite" and not rewritten:
        raise ValueError("rewrite requires --rewritten-content")
    if decision != "rewrite" and rewritten:
        raise ValueError("--rewritten-content is only allowed for rewrite")
    if decision == "merge" and not target:
        raise ValueError("merge requires --merge-target-id")
    if decision != "merge" and target:
        raise ValueError("--merge-target-id is only allowed for merge")
    if decision in {"keep", "rewrite"} and not source_ids:
        raise ValueError(f"{decision} requires at least one --source-event-id")
    if source_ids and evidence_confidence == "none":
        raise ValueError("bound source events require non-none evidence confidence")
    if not source_ids and evidence_confidence != "none":
        raise ValueError("non-none evidence confidence requires bound source events")

    candidate_rows = copy.deepcopy(rows)
    candidate = candidate_rows[row_index]
    candidate["decision"] = decision
    candidate["review_status"] = "reviewed"
    candidate["rewritten_content"] = rewritten if decision == "rewrite" else None
    candidate["merge_target_id"] = target if decision == "merge" else None
    candidate["source_event_ids"] = source_ids
    candidate["evidence_confidence"] = evidence_confidence
    candidate["reviewer"] = reviewer
    candidate["reviewed_at"] = parse_time(reviewed_at).isoformat()
    candidate["validation"] = {"status": "reviewed", "errors": []}

    validation = _validate_queue_rows(candidate_rows, events)
    if not validation["ok"]:
        row_errors = [error for error in validation["errors"] if error.get("line") == row_index + 1]
        if row_errors:
            raise ValueError(f"review would violate queue contract: {row_errors[0]['errors']}")
        raise ValueError(f"queue contains pre-existing validation errors: {validation['errors'][:3]}")
    rows[row_index] = candidate
    return {"legacy_summary_id": candidate["legacy_summary_id"], "decision": decision, "validation": validation}


def _print_inspection(payload: dict[str, Any]) -> None:
    print(f"[{payload['queue_index']}/{payload['queue_rows']}] {payload['legacy_summary_id']}")
    print(f"created_at: {payload['created_at']}")
    print(f"decision: {payload['decision'] or '<pending>'}")
    print(f"summary: {payload['original_content']}")
    print("\nEvidence candidates (hints only; bind IDs only after reading the raw text):")
    for candidate in payload["evidence_candidates"]:
        print(
            f"\n#{candidate['rank']} {candidate['source_event_id']} "
            f"role={candidate.get('role')} overlap={candidate.get('lexical_overlap')} "
            f"delta={candidate.get('time_delta_seconds')}s conversation={candidate.get('conversation_id')}"
        )
        if candidate["text"] is None:
            print("<raw event missing>")
        else:
            print(candidate["text"])
            if candidate["text_truncated"]:
                print("<truncated; rerun with a larger --max-chars>")


def cmd_init(args: argparse.Namespace) -> int:
    print(json.dumps(initialize_working_queue(args.source, args.output), ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    rows = read_jsonl(args.queue)
    events = _load_raw_events(args.raw_db)
    validation = _validate_queue_rows(rows, events)
    print(json.dumps({**queue_status(rows), "validation": validation}, ensure_ascii=False, indent=2))
    return 0 if validation["ok"] else 1


def cmd_show(args: argparse.Namespace) -> int:
    payload = inspect_row(
        read_jsonl(args.queue),
        _load_raw_events(args.raw_db),
        summary_id=args.id,
        index=args.index,
        next_pending=args.next_pending,
        candidate_limit=args.candidate_limit,
        max_chars=args.max_chars,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_inspection(payload)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    queue_path = Path(args.queue).resolve()
    rows = read_jsonl(str(queue_path))
    events = _load_raw_events(args.raw_db)
    reviewed_at = args.reviewed_at or datetime.now(timezone.utc).isoformat()
    result = apply_review(
        rows,
        events,
        summary_id=args.id,
        decision=args.decision,
        reviewer=args.reviewer,
        reviewed_at=reviewed_at,
        source_event_ids=args.source_event_id,
        evidence_confidence=args.evidence_confidence,
        rewritten_content=args.rewritten_content,
        merge_target_id=args.merge_target_id,
    )
    _atomic_write_jsonl(queue_path, rows)
    payload = {"ok": True, "queue": str(queue_path), **result, **queue_status(rows)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect raw evidence and record audited legacy summary decisions")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a separate working copy of the generated baseline queue")
    init.add_argument("--source", required=True)
    init.add_argument("--output", required=True)
    init.set_defaults(handler=cmd_init)

    status = sub.add_parser("status", help="show decision counts and validate the working queue")
    status.add_argument("--queue", required=True)
    status.add_argument("--raw-db", required=True)
    status.set_defaults(handler=cmd_status)

    show = sub.add_parser("show", help="show one summary and the raw text behind its candidate hints")
    show.add_argument("--queue", required=True)
    show.add_argument("--raw-db", required=True)
    show.add_argument("--id")
    show.add_argument("--index", type=int)
    show.add_argument("--next-pending", action="store_true")
    show.add_argument("--candidate-limit", type=int, default=12)
    show.add_argument("--max-chars", type=int, default=4000)
    show.add_argument("--json", action="store_true")
    show.set_defaults(handler=cmd_show)

    set_review = sub.add_parser("set", help="atomically record one reviewed decision in a working queue")
    set_review.add_argument("--queue", required=True)
    set_review.add_argument("--raw-db", required=True)
    set_review.add_argument("--id", required=True)
    set_review.add_argument("--decision", required=True, choices=sorted(ALLOWED_DECISIONS))
    set_review.add_argument("--source-event-id", action="append")
    set_review.add_argument("--evidence-confidence", choices=sorted(ALLOWED_CONFIDENCE), default="none")
    set_review.add_argument("--rewritten-content")
    set_review.add_argument("--merge-target-id")
    set_review.add_argument("--reviewer", required=True)
    set_review.add_argument("--reviewed-at", help="ISO-8601 timestamp; defaults to current UTC time")
    set_review.set_defaults(handler=cmd_set)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"summary review failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
