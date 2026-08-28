"""Letter tools implemented on Haven's BucketManager.

This is a behavioral port of P0luz's letter contract.  It uses two-step
create/update storage because Haven's create() keeps its stable public
signature; all lock changes go through the bucket-level CAS mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

from bucket_manager import BucketManager
from letter_lock import (
    AI_AUTHOR_ALIASES,
    HUMAN_AUTHOR_ALIASES,
    GENERIC_RELATION_NAMES,
    author_side,
    is_letter_bucket,
    letter_lock_revision,
    letter_lock_state,
    normalize_expired_lock,
    normalize_lock_type,
    normalize_unlock_date,
    resolve_writer_name,
    safe_letter_metadata,
)
from utils import now_iso, strip_wikilinks


_CONTENT_MAX = 100_000
_TITLE_MAX = 120
_REASON_MAX = 500


def _ai_name() -> str:
    return os.environ.get("AI_NAME", "AI").strip() or "AI"


def _owner_name() -> str:
    return os.environ.get("OMBRE_OWNER_NAME", "").strip()


async def letter_write(
    bucket_manager: BucketManager,
    author: str,
    content: str,
    user_name: str = "",
    title: str = "",
    date: str = "",
    ai_name: str = "",
    lock_type: str = "none",
    unlock_date: str = "",
) -> str:
    author_value = str(author or "").strip()
    content_value = str(content or "").strip()
    if not author_value:
        raise ValueError("author 不能为空")
    if not content_value:
        raise ValueError("信件正文不能为空")
    if len(content_value) > _CONTENT_MAX:
        raise ValueError(f"信件正文过长（上限 {_CONTENT_MAX} 字）")

    ai_name_value = str(ai_name or "").strip() or _ai_name()
    side = author_side(author_value, ai_name=ai_name_value)
    normalized_lock = normalize_lock_type(lock_type)
    normalized_unlock = normalize_unlock_date(
        normalized_lock,
        unlock_date,
        now=datetime.now(timezone.utc),
    )
    if normalized_lock != "none":
        if side != "ai":
            raise ValueError(
                "无法创建带锁 Letter：当前 MCP/stdio 入口不能替对方创建带锁信。"
                "无锁代存仍然可用。"
            )
        writer_name = resolve_writer_name(
            side,
            author_value,
            user_name=user_name,
            ai_name=ai_name_value,
        )
        if not writer_name:
            raise ValueError(
                "未能创建带锁 Letter：未能取得当前写信人的实际关系名。"
            )

    if side == "ai":
        stored_author = ai_name_value
    elif side == "human":
        stored_author = "user"
    else:
        stored_author = author_value
    title_value = str(title or "").strip()[:_TITLE_MAX]
    writer_name = resolve_writer_name(
        side or "ai",
        author_value,
        user_name=user_name,
        ai_name=ai_name_value,
    )
    letter_date = str(date or "").strip() or now_iso()[:10]
    name = title_value or f"{stored_author}_{letter_date}"

    bucket_id = await bucket_manager.create(
        content=content_value,
        tags=["__letter__"],
        importance=10,
        domain=["letter"],
        valence=0.5,
        arousal=0.3,
        bucket_type="letter",
        name=name,
        pinned=False,
        protected=False,
        source=None,
        created=now_iso(),
        extra_metadata={
            "source_tool": "letter",
            "event_actor": "llm",
            "author": stored_author,
            "user_name": str(user_name or "").strip(),
            "title": title_value[:_TITLE_MAX],
            "letter_date": letter_date,
            "lock_type": normalized_lock,
            "unlock_date": normalized_unlock,
            "locked_by": side if normalized_lock != "none" else "",
            "writer_name": writer_name or stored_author,
        },
    )

    suffix = ""
    if normalized_lock != "none":
        suffix = f" 🔒{normalized_lock} 解锁:{normalized_unlock}"
    return f"💌letter→{bucket_id} [{stored_author}]{suffix}"


async def letter_lock_update(
    bucket_manager: BucketManager,
    letter_id: str,
    lock_type: str,
    unlock_date: str,
    caller_side: str = "ai",
) -> str:
    try:
        target_lock = normalize_lock_type(lock_type)
        target_unlock = normalize_unlock_date(
            target_lock,
            unlock_date,
            now=datetime.now(timezone.utc),
        )
    except ValueError as exc:
        return f"无法修改 Letter 锁：{exc}"

    bucket = await bucket_manager.get(str(letter_id or "").strip())
    if not bucket or not is_letter_bucket(bucket):
        return "未找到该 Letter"
    metadata = bucket.get("metadata") or {}
    revision = letter_lock_revision(bucket)
    if not metadata.get("locked_by"):
        return (
            "历史无锁 Letter 没有锁所有者，不能通过锁管理入口补设锁。"
            "请新写一封带锁 Letter。"
        )
    owner = metadata.get("locked_by")
    if owner != caller_side:
        return "只有创建这把锁的一方可以修改 Letter 锁状态。"

    author = metadata.get("author")
    current_side = author_side(author, ai_name=_ai_name())
    writer_name = str(metadata.get("writer_name") or "")
    # A locked letter can be relocked by its owner even if legacy or mutated
    # metadata no longer resolves its author to the trusted caller side.
    is_unlocked_letter = str(metadata.get("lock_type") or "none") == "none"
    if target_lock != "none" and is_unlocked_letter:
        if current_side != caller_side:
            return (
                "无法上锁：这封无锁 Letter 的署名方向与当前可信入口不一致；"
                "代存信不能事后转换为锁信。"
            )
        if not writer_name or writer_name.lower() in GENERIC_RELATION_NAMES:
            return (
                "无法上锁：这封 Letter 创建时没有记录实际关系名，"
                "请新写一封带锁 Letter。"
            )

    async def mutation(post):
        current_revision = letter_lock_revision({"metadata": dict(post.metadata)})
        if current_revision != revision:
            return False, {"ok": False, "conflict": True}
        post["lock_type"] = target_lock
        post["unlock_date"] = target_unlock
        return True, {"ok": True, "conflict": False}

    result = await bucket_manager.mutate_lock_fields(letter_id, mutation)
    if not result or not result.get("ok"):
        refreshed = await bucket_manager.get(letter_id)
        refreshed_revision = letter_lock_revision(refreshed) if refreshed else None
        if refreshed_revision != revision:
            return "Letter 锁状态已被并发修改，请重新读取后再试"
        return "无法修改 Letter 锁"
    refreshed = await bucket_manager.get(letter_id)
    state = letter_lock_state(refreshed, caller_side=caller_side) if refreshed else {}
    if target_lock == "none":
        return f"🔓 已解锁 {bucket['id']}，恢复默认可读。"
    if target_lock == "permanent":
        return f"🔒 已将 {bucket['id']} 设为永久锁。"
    return f"🔒 已将 {bucket['id']} 设为定时锁，解锁日期：{target_unlock}。"


def _matches_query(bucket: dict, query: str) -> bool:
    if not query:
        return True
    needle = query.lower()
    metadata = bucket.get("metadata") or {}
    haystacks = [
        bucket.get("content") or "",
        metadata.get("name") or "",
        metadata.get("title") or "",
        metadata.get("author") or "",
        metadata.get("writer_name") or "",
        " ".join(metadata.get("tags") or []),
    ]
    return any(needle in str(item).lower() for item in haystacks)


async def letter_read(
    bucket_manager: BucketManager,
    query: str = "",
    limit: int = 10,
    author: str = "",
    date_from: str = "",
    date_to: str = "",
    caller_side: str = "ai",
) -> str:
    if not isinstance(limit, int) or limit < 1:
        raise ValueError("limit 必须是 1 到 50 之间的整数")
    if limit > 50:
        raise ValueError("limit 必须是 1 到 50 之间的整数")
    all_buckets = await bucket_manager.list_all(include_archive=False)
    ai_name = _ai_name()
    letters = []
    for bucket in all_buckets:
        if not is_letter_bucket(bucket):
            continue
        bucket, state = await normalize_expired_lock(
            bucket_manager,
            bucket,
            caller_side,
        )
        if author:
            author_value = str(author).strip()
            actual_author = str(
                (bucket.get("metadata") or {}).get("writer_name")
                or (bucket.get("metadata") or {}).get("author")
                or ""
            )
            if (
                author_value.lower() not in AI_AUTHOR_ALIASES
                and author_value.lower() not in HUMAN_AUTHOR_ALIASES
                and author_value.lower() not in {ai_name.lower()}
                and actual_author.lower() != author_value.lower()
            ):
                continue
            if author_value.lower() in HUMAN_AUTHOR_ALIASES and str(
                (bucket.get("metadata") or {}).get("author") or ""
            ).lower() != "user":
                continue
            if (
                author_value.lower() in AI_AUTHOR_ALIASES
                or author_value.lower() == ai_name.lower()
            ) and str((bucket.get("metadata") or {}).get("author") or "").lower() not in {
                "ai",
                ai_name.lower(),
            }:
                continue
        date_value = str(
            (bucket.get("metadata") or {}).get("letter_date")
            or (bucket.get("metadata") or {}).get("created")
            or ""
        )[:10]
        if date_from and date_value < date_from:
            continue
        if date_to and date_value > date_to:
            continue
        letters.append((bucket, state))

    if query:
        candidates = []
        for bucket, state in letters:
            if state.get("locked"):
                continue
            if _matches_query(bucket, query):
                candidates.append((bucket, state))
    else:
        candidates = sorted(
            letters,
            key=lambda item: str(
                (item[0].get("metadata") or {}).get("letter_date")
                or (item[0].get("metadata") or {}).get("created")
                or ""
            ),
            reverse=True,
        )[:limit]

    if query:
        candidates = sorted(
            candidates,
            key=lambda item: str(
                (item[0].get("metadata") or {}).get("letter_date")
                or (item[0].get("metadata") or {}).get("created")
                or ""
            ),
            reverse=True,
        )[:limit]

    if not candidates:
        return "没有找到匹配的信件。"
    chunks = []
    for bucket, state in candidates:
        if state.get("locked"):
            safe = safe_letter_metadata(bucket, state)
            chunks.append(
                f"{bucket.get('id')} · 一封上锁的信 · "
                f"解锁:{safe.get('unlock_date') or '未定'}"
            )
            continue
        metadata = bucket.get("metadata") or {}
        letter_date = str(metadata.get("letter_date") or metadata.get("created") or "")[:10]
        title = str(metadata.get("title") or "").strip()
        writer = str(metadata.get("writer_name") or metadata.get("author") or "").strip()
        chunks.append(
            f"{bucket.get('id')} {writer} · {letter_date}"
            + (f" · {title}" if title else "")
            + f"\n{strip_wikilinks(bucket.get('content') or '')}"
        )
    return "=== 信件 ===\n" + "\n\n---\n\n".join(chunks)
