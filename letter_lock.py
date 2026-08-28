"""Letter lock helpers and metadata contract.

Letters are special buckets whose lifecycle can survive type rewrites (for
example, archiving overwrites ``type``), so recognition uses multiple stable
markers instead of trusting one field alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any, Optional


LETTER_LOCK_TYPES = {"none", "timed", "permanent"}
PERMANENT_UNLOCK_DATE = "9999-12-31"
GENERIC_RELATION_NAMES = {
    "ai",
    "a.i.",
    "assistant",
    "claude",
    "bot",
    "model",
    "user",
    "human",
    "human-side",
    "ai-side",
    "you",
    "me",
}
HUMAN_AUTHOR_ALIASES = {"user", "human", "human-side"}
AI_AUTHOR_ALIASES = {"ai", "ai-side", "claude"}


def normalize_lock_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in LETTER_LOCK_TYPES:
        raise ValueError("lock_type 必须是 none、timed 或 permanent")
    return normalized


def normalize_unlock_date(lock_type: str, value: Any, now: datetime | None = None) -> str | None:
    if lock_type == "none":
        return None
    if lock_type == "permanent":
        return PERMANENT_UNLOCK_DATE
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("定时锁解锁时间必须是带时区的 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError("定时锁解锁时间必须带时区")
    current = now or datetime.now(timezone.utc)
    if parsed <= current:
        raise ValueError("定时锁解锁时间必须晚于当前时间")
    return parsed.isoformat()


def author_side(author: Any, ai_name: str = "") -> Optional[str]:
    value = str(author or "").strip().lower()
    if value in HUMAN_AUTHOR_ALIASES:
        return "human"
    if value in AI_AUTHOR_ALIASES or (ai_name and value == str(ai_name).strip().lower()):
        return "ai"
    return None


def _actual_name_for_side(
    side: str,
    author: Any,
    user_name: str = "",
    ai_name: str = "",
) -> str:
    author_value = str(author or "").strip()
    if side == "ai":
        for candidate in (ai_name, os.environ.get("AI_NAME", ""), author_value):
            candidate = candidate.strip()
            if candidate and candidate.lower() not in GENERIC_RELATION_NAMES:
                return candidate
        return ""
    for candidate in (
        user_name,
        os.environ.get("OMBRE_OWNER_NAME", ""),
        author_value,
    ):
        candidate = candidate.strip()
        if candidate and candidate.lower() not in GENERIC_RELATION_NAMES:
            return candidate
    return ""


def resolve_writer_name(
    caller_side: str,
    author: Any,
    user_name: str = "",
    ai_name: str = "",
) -> str:
    if caller_side == "human":
        return _actual_name_for_side("human", author, user_name=user_name)
    return _actual_name_for_side("ai", author, ai_name=ai_name)


def is_letter_bucket(bucket: dict | None) -> bool:
    if not bucket:
        return False
    metadata = bucket.get("metadata") or {}
    tags = metadata.get("tags") or []
    return (
        str(metadata.get("type") or "") == "letter"
        or str(metadata.get("source_tool") or "") == "letter"
        or "__letter__" in tags
        or (
            str(metadata.get("locked_by") or "") in {"human", "ai"}
            and str(metadata.get("lock_type") or "") in {"timed", "permanent"}
        )
    )


def letter_lock_revision(bucket: dict | None) -> tuple[str, str | None, str]:
    metadata = (bucket or {}).get("metadata") or {}
    return (
        str(metadata.get("lock_type") or ""),
        metadata.get("unlock_date"),
        str(metadata.get("locked_by") or ""),
    )


def letter_lock_state(
    bucket: dict | None,
    caller_side: str = "ai",
    now: datetime | None = None,
) -> dict:
    metadata = (bucket or {}).get("metadata") or {}
    stored_lock_type = str(metadata.get("lock_type") or "none")
    unlock_date = metadata.get("unlock_date")
    locked_by = str(metadata.get("locked_by") or "")
    expired = False
    if stored_lock_type == "timed" and unlock_date:
        try:
            expires_at = datetime.fromisoformat(str(unlock_date).replace("Z", "+00:00"))
        except ValueError:
            expires_at = None
        if expires_at is not None:
            current = now or datetime.now(timezone.utc)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            expired = current >= expires_at
    effective = "none" if expired else (stored_lock_type or "none")
    owner = locked_by if locked_by in {"human", "ai"} else ""
    return {
        "lock_type": effective,
        "stored_lock_type": stored_lock_type,
        "unlock_date": unlock_date,
        "locked_by": locked_by,
        "owner": owner == caller_side,
        "locked": effective in {"timed", "permanent"} and owner != caller_side,
        "expired": expired,
    }


async def normalize_expired_lock(bucket_manager, bucket: dict, caller_side: str = "ai") -> tuple[dict, dict]:
    state = letter_lock_state(bucket, caller_side=caller_side)
    if not state["expired"]:
        return bucket, state
    bucket_id = bucket.get("id")
    revision = letter_lock_revision(bucket)

    async def mutation(post):
        if letter_lock_revision({"metadata": dict(post.metadata)}) != revision:
            return False, {"ok": False, "conflict": True, "state": state}
        post["lock_type"] = "none"
        post["unlock_date"] = None
        return True, {"ok": True, "conflict": False}

    result = await bucket_manager.mutate_lock_fields(bucket.get("id"), mutation)
    if result and result.get("ok") and not result.get("conflict"):
        refreshed = await bucket_manager.get(bucket_id)
        if refreshed:
            return refreshed, letter_lock_state(refreshed, caller_side=caller_side)
    return bucket, state


def safe_letter_metadata(bucket: dict | None, state: dict | None = None) -> dict:
    metadata = (bucket or {}).get("metadata") or {}
    content = (bucket or {}).get("content") or ""
    lock = state or letter_lock_state(bucket)
    locked = bool(lock.get("locked"))
    payload = {
        "letter_id": bucket.get("id"),
        "author": metadata.get("writer_name") or metadata.get("author") or "",
        "created_at": metadata.get("created"),
        "lock_type": lock.get("lock_type") or "none",
        "unlock_date": lock.get("unlock_date"),
        "locked_by": lock.get("locked_by") or "",
        "locked": locked,
        "lock_upgrade_available": not bool(metadata.get("lock_type")),
    }
    if not locked:
        payload.update(
            {
                "title": metadata.get("title"),
                "letter_date": metadata.get("letter_date"),
                "content": content,
            }
        )
    return payload
