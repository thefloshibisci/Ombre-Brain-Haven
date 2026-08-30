"""Rebuild legacy summary evidence candidates using causal cron batch windows.

The legacy Supabase summaries were written by a cron job that fired roughly every
six hours, so a summary's ``created_at`` is a batch write time rather than the
time of the conversation it describes. The first review queue ranked candidates
inside a symmetric +/-36h window, which let events that happened *after* the
summary was written outrank the real sources.

This script produces a separate, read-only candidate artifact that:

* keeps only canonical raw events (timezone shadows are dropped via the
  duplicate map, so the same turn never appears twice),
* keeps only events that precede the summary, and
* bounds each summary to the cron interval that produced it.

It never mutates the raw archive, the baseline queue, or a working queue.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from scripts.prepare_summary_review_queue import (
        _load_artifact,
        _load_raw_events,
        lexical_score,
        normalized_text,
        parse_time,
        tokens,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from prepare_summary_review_queue import (
        _load_artifact,
        _load_raw_events,
        lexical_score,
        normalized_text,
        parse_time,
        tokens,
    )

SCHEMA_VERSION = "ombre-summary-evidence-window-v1"
DUPLICATE_MAP_SCHEMA = "ombre-raw-duplicate-map-v1"
DEFAULT_BATCH_GAP_SECONDS = 300
DEFAULT_MAX_LOOKBACK_HOURS = 12
DEFAULT_MAX_CANDIDATES = 12


def load_shadow_ids(path: str | None) -> set[str]:
    if not path:
        return set()
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != DUPLICATE_MAP_SCHEMA:
        raise ValueError(f"unsupported duplicate map schema: {payload.get('schema_version')!r}")
    return {normalized_text(sid) for sid in payload.get("shadow_source_event_ids") or [] if normalized_text(sid)}


def group_batches(items: list[dict[str, Any]], gap_seconds: int = DEFAULT_BATCH_GAP_SECONDS) -> list[list[int]]:
    """Group summary indices into cron write bursts, ordered by write time."""

    timed: list[tuple[datetime, int]] = []
    for index, item in enumerate(items):
        created = parse_time(item.get("created_at"))
        if created is None:
            raise ValueError(f"summary {item.get('legacy_summary_id')} has an unparsable created_at")
        timed.append((created, index))
    timed.sort()

    batches: list[list[int]] = []
    current: list[int] = []
    previous: datetime | None = None
    for created, index in timed:
        if previous is not None and (created - previous).total_seconds() > gap_seconds:
            batches.append(current)
            current = []
        current.append(index)
        previous = created
    if current:
        batches.append(current)
    return batches


def _window_bounds(
    batch_starts: list[datetime],
    batch_number: int,
    earliest_event: datetime | None,
    max_lookback: timedelta,
) -> tuple[datetime | None, str]:
    start = batch_starts[batch_number]
    if batch_number == 0:
        # The first burst backfilled every conversation that already existed.
        return earliest_event, "initial_backfill"
    previous = batch_starts[batch_number - 1]
    if start - previous > max_lookback:
        return start - max_lookback, "clamped_gap"
    return previous, "cron_interval"


def build_evidence_windows(
    items: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    shadow_ids: set[str] | None = None,
    batch_gap_seconds: int = DEFAULT_BATCH_GAP_SECONDS,
    max_lookback_hours: int = DEFAULT_MAX_LOOKBACK_HOURS,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> dict[str, Any]:
    shadow_ids = shadow_ids or set()
    max_lookback = timedelta(hours=max_lookback_hours)

    canonical = [
        event
        for event in events
        if event["created_time"] is not None and event["source_event_id"] not in shadow_ids
    ]
    canonical.sort(key=lambda event: (event["created_time"], event["source_event_id"]))
    event_times = [event["created_time"] for event in canonical]
    earliest_event = event_times[0] if event_times else None

    batches = group_batches(items, batch_gap_seconds)
    batch_starts = [parse_time(items[batch[0]].get("created_at")) for batch in batches]

    rows: list[dict[str, Any]] = []
    stats = {
        "summaries": len(items),
        "batches": len(batches),
        "canonical_events": len(canonical),
        "dropped_shadow_events": len(events) - len(canonical),
        "empty_window_summaries": 0,
        "no_candidate_summaries": 0,
        "window_kinds": {},
        "top_overlap_buckets": {"none": 0, "<0.25": 0, "0.25-0.4": 0, "0.4-0.6": 0, ">=0.6": 0},
    }

    for batch_number, batch in enumerate(batches):
        window_start, window_kind = _window_bounds(batch_starts, batch_number, earliest_event, max_lookback)
        stats["window_kinds"][window_kind] = stats["window_kinds"].get(window_kind, 0) + 1
        for index in batch:
            item = items[index]
            summary_time = parse_time(item.get("created_at"))
            summary_tokens = tokens(str(item.get("legacy_content") or ""))
            assistant_id = normalized_text(item.get("assistant_id"))

            low = 0 if window_start is None else bisect.bisect_left(event_times, window_start)
            high = bisect.bisect_right(event_times, summary_time)
            window_events = canonical[low:high]

            candidates: list[dict[str, Any]] = []
            for event in window_events:
                lexical = lexical_score(summary_tokens, event["text"])
                assistant_match = bool(assistant_id and assistant_id == event["assistant_id"])
                if lexical == 0.0:
                    continue
                candidates.append(
                    {
                        "source_event_id": event["source_event_id"],
                        "created_at": event["created_at"],
                        "role": event["role"],
                        "conversation_id": event["conversation_id"],
                        "assistant_match": assistant_match,
                        "seconds_before_summary": int((summary_time - event["created_time"]).total_seconds()),
                        "lexical_overlap": lexical,
                    }
                )
            candidates.sort(
                key=lambda row: (
                    -float(row["lexical_overlap"]),
                    int(row["seconds_before_summary"]),
                    row["source_event_id"],
                )
            )
            candidates = candidates[:max_candidates]

            if not window_events:
                stats["empty_window_summaries"] += 1
            if not candidates:
                stats["no_candidate_summaries"] += 1

            top = candidates[0]["lexical_overlap"] if candidates else None
            if top is None:
                stats["top_overlap_buckets"]["none"] += 1
            elif top >= 0.6:
                stats["top_overlap_buckets"][">=0.6"] += 1
            elif top >= 0.4:
                stats["top_overlap_buckets"]["0.4-0.6"] += 1
            elif top >= 0.25:
                stats["top_overlap_buckets"]["0.25-0.4"] += 1
            else:
                stats["top_overlap_buckets"]["<0.25"] += 1

            rows.append(
                {
                    "legacy_summary_id": normalized_text(item.get("legacy_summary_id")),
                    "summary_created_at": item.get("created_at"),
                    "batch_number": batch_number,
                    "batch_size": len(batch),
                    "window_kind": window_kind,
                    "window_start": window_start.isoformat() if window_start else None,
                    "window_end": summary_time.isoformat(),
                    "window_event_count": len(window_events),
                    "window_conversations": sorted({event["conversation_id"] for event in window_events}),
                    "evidence_candidates": candidates,
                }
            )

    rows.sort(key=lambda row: (row["summary_created_at"], row["legacy_summary_id"]))
    ids = [row["legacy_summary_id"] for row in rows]
    errors: list[str] = []
    if len(set(ids)) != len(ids):
        errors.append("duplicate legacy_summary_id in evidence windows")
    if any(not row_id for row_id in ids):
        errors.append("evidence window row is missing legacy_summary_id")
    for row in rows:
        for candidate in row["evidence_candidates"]:
            if candidate["seconds_before_summary"] < 0:
                errors.append(f"candidate {candidate['source_event_id']} postdates summary {row['legacy_summary_id']}")
                break
            if candidate["source_event_id"] in shadow_ids:
                errors.append(f"candidate {candidate['source_event_id']} is a timezone shadow")
                break

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not errors,
        "parameters": {
            "batch_gap_seconds": batch_gap_seconds,
            "max_lookback_hours": max_lookback_hours,
            "max_candidates": max_candidates,
        },
        "stats": stats,
        "errors": errors[:20],
        "items": rows,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", delete=False, dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp",
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild causal evidence windows for legacy summaries")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--raw-db", required=True)
    parser.add_argument("--duplicate-map")
    parser.add_argument("--output", required=True)
    parser.add_argument("--batch-gap-seconds", type=int, default=DEFAULT_BATCH_GAP_SECONDS)
    parser.add_argument("--max-lookback-hours", type=int, default=DEFAULT_MAX_LOOKBACK_HOURS)
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    args = parser.parse_args(argv)

    try:
        _, items = _load_artifact(args.artifact)
        events = _load_raw_events(args.raw_db)
        payload = build_evidence_windows(
            items,
            events,
            shadow_ids=load_shadow_ids(args.duplicate_map),
            batch_gap_seconds=args.batch_gap_seconds,
            max_lookback_hours=args.max_lookback_hours,
            max_candidates=args.max_candidates,
        )
        _atomic_write_json(Path(args.output).resolve(), payload)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"evidence window rebuild failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(
        {
            "ok": payload["ok"],
            "output": str(Path(args.output).resolve()),
            "parameters": payload["parameters"],
            "stats": payload["stats"],
            "errors": payload["errors"],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())