"""Audit duplicate raw events created by a timezone-shifted legacy export.

The staging raw archive faithfully mirrors the Supabase export, and that export
contains a second copy of many messages whose local Asia/Shanghai wall-clock was
recorded as if it were UTC. The archive must stay immutable, so this script only
produces a canonical/shadow map that later stages (evidence binding, import,
embedding) can use to avoid double counting.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "ombre-raw-duplicate-map-v1"
DEFAULT_SHIFT_SECONDS = 8 * 3600
DEFAULT_TOLERANCE_SECONDS = 2


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _has_subsecond(value: str | None) -> bool:
    return "." in (value or "")


def load_events(raw_db: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{Path(raw_db).resolve().as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(
            "select id, source_event_id, conversation_id, role, text, created_at, metadata_json from raw_events"
        )]
    finally:
        conn.close()
    events: list[dict[str, Any]] = []
    for row in rows:
        created = parse_time(row.get("created_at"))
        if created is None:
            raise ValueError(f"raw event {row.get('source_event_id')} has an unparsable created_at")
        events.append(
            {
                "id": row["id"],
                "source_event_id": str(row["source_event_id"] or "").strip(),
                "conversation_id": str(row["conversation_id"] or "").strip(),
                "role": str(row["role"] or "").strip(),
                "created_at": str(row["created_at"]),
                "created_dt": created,
                "text_sha256": hashlib.sha256((row["text"] or "").encode("utf-8")).hexdigest(),
                "text_length": len(row["text"] or ""),
                "has_subsecond": _has_subsecond(row.get("created_at")),
            }
        )
    missing = [event for event in events if not event["source_event_id"]]
    if missing:
        raise ValueError(f"{len(missing)} raw events have no source_event_id")
    return events


def _pick_canonical(a: dict[str, Any], b: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    if a["has_subsecond"] != b["has_subsecond"]:
        canonical, shadow = (a, b) if a["has_subsecond"] else (b, a)
        return canonical, shadow, "subsecond_precision"
    canonical, shadow = (a, b) if a["created_dt"] <= b["created_dt"] else (b, a)
    return canonical, shadow, "earliest_timestamp"


def build_duplicate_map(
    events: list[dict[str, Any]],
    *,
    shift_seconds: int = DEFAULT_SHIFT_SECONDS,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    shift = timedelta(seconds=shift_seconds)
    tolerance = timedelta(seconds=tolerance_seconds)

    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for event in events:
        buckets[(event["conversation_id"], event["role"], event["text_sha256"])].append(event)

    clusters: list[dict[str, Any]] = []
    repeats: list[dict[str, Any]] = []
    for key, members in buckets.items():
        members = sorted(members, key=lambda item: (item["created_dt"], item["id"]))
        paired: set[str] = set()
        for index, earlier in enumerate(members):
            if earlier["source_event_id"] in paired:
                continue
            for later in members[index + 1 :]:
                if later["source_event_id"] in paired:
                    continue
                if abs((later["created_dt"] - earlier["created_dt"]) - shift) <= tolerance:
                    canonical, shadow, rule = _pick_canonical(earlier, later)
                    clusters.append(
                        {
                            "conversation_id": key[0],
                            "role": key[1],
                            "text_sha256": key[2],
                            "text_length": canonical["text_length"],
                            "canonical_source_event_id": canonical["source_event_id"],
                            "canonical_created_at": canonical["created_at"],
                            "shadow_source_event_ids": [shadow["source_event_id"]],
                            "shadow_created_at": [shadow["created_at"]],
                            "offset_seconds": round((later["created_dt"] - earlier["created_dt"]).total_seconds()),
                            "canonical_rule": rule,
                        }
                    )
                    paired.add(earlier["source_event_id"])
                    paired.add(later["source_event_id"])
                    break
        leftovers = [member for member in members if member["source_event_id"] not in paired]
        if len(leftovers) > 1:
            repeats.append(
                {
                    "conversation_id": key[0],
                    "role": key[1],
                    "text_sha256": key[2],
                    "text_length": leftovers[0]["text_length"],
                    "source_event_ids": [member["source_event_id"] for member in leftovers],
                    "created_at": [member["created_at"] for member in leftovers],
                    "max_gap_seconds": round(
                        (leftovers[-1]["created_dt"] - leftovers[0]["created_dt"]).total_seconds()
                    ),
                }
            )

    clusters.sort(key=lambda item: (item["conversation_id"], item["canonical_created_at"]))
    repeats.sort(key=lambda item: (item["conversation_id"], item["created_at"][0]))

    shadow_ids = sorted({sid for cluster in clusters for sid in cluster["shadow_source_event_ids"]})
    canonical_ids = {cluster["canonical_source_event_id"] for cluster in clusters}
    overlap = sorted(canonical_ids & set(shadow_ids))

    conversation_stats: dict[str, dict[str, int]] = {}
    per_conversation_total = collections.Counter(event["conversation_id"] for event in events)
    per_conversation_shadow = collections.Counter(
        cluster["conversation_id"] for cluster in clusters
    )
    for conversation_id, total in per_conversation_total.items():
        conversation_stats[conversation_id] = {
            "events": total,
            "shadow_events": per_conversation_shadow.get(conversation_id, 0),
        }

    rule_counts = collections.Counter(cluster["canonical_rule"] for cluster in clusters)

    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not overlap,
        "shift_seconds": shift_seconds,
        "tolerance_seconds": tolerance_seconds,
        "totals": {
            "raw_events": len(events),
            "shadow_clusters": len(clusters),
            "shadow_events": len(shadow_ids),
            "canonical_events": len(events) - len(shadow_ids),
            "conversations": len(per_conversation_total),
            "conversations_with_shadows": sum(1 for value in per_conversation_shadow.values() if value),
            "unresolved_repeat_groups": len(repeats),
        },
        "canonical_rules": dict(rule_counts),
        "errors": [f"event {sid} is both canonical and shadow" for sid in overlap],
        "conversations": conversation_stats,
        "shadow_source_event_ids": shadow_ids,
        "clusters": clusters,
        "unresolved_repeats": repeats,
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
    parser = argparse.ArgumentParser(description="Map timezone-shifted duplicate raw events without mutating the archive")
    parser.add_argument("--raw-db", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--shift-seconds", type=int, default=DEFAULT_SHIFT_SECONDS)
    parser.add_argument("--tolerance-seconds", type=int, default=DEFAULT_TOLERANCE_SECONDS)
    args = parser.parse_args(argv)

    try:
        events = load_events(args.raw_db)
        payload = build_duplicate_map(
            events,
            shift_seconds=args.shift_seconds,
            tolerance_seconds=args.tolerance_seconds,
        )
        _atomic_write_json(Path(args.output).resolve(), payload)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"raw duplicate audit failed: {exc}", file=sys.stderr)
        return 1

    summary = {
        "ok": payload["ok"],
        "output": str(Path(args.output).resolve()),
        "totals": payload["totals"],
        "canonical_rules": payload["canonical_rules"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())