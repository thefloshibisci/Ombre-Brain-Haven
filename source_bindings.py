"""Reversible, evidence-only Source V1 binding operations."""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any

from source_store import (
    MAX_SOURCE_LINKS,
    MAX_SOURCE_REFS,
    active_source_refs_from_links,
    normalize_source_ranges,
    source_links_from_metadata,
)


def _normalized_title(value: object) -> str:
    # Haven does not yet enforce a hard title cap.  Preserve the P0luz
    # behavior contract without introducing a destructive format migration.
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split())


def _key(link: dict[str, Any]) -> tuple[str, tuple[tuple[int, int], ...]]:
    return link["ref"], tuple(tuple(pair) for pair in link["ranges"])


def _metadata(post: Any) -> dict[str, Any]:
    if isinstance(post, dict):
        value = post.get("metadata") or {}
    else:
        value = getattr(post, "metadata", {}) or {}
    return value if isinstance(value, dict) else {}


def _title(post: Any) -> object:
    metadata = _metadata(post)
    if "title" in metadata:
        return metadata.get("title")
    getter = getattr(post, "get", None)
    return getter("title") if callable(getter) else None


def _access_error(post: Any, expected_title: str) -> str:
    actual = _normalized_title(_title(post))
    if not actual:
        return "该桶没有可供精确校验的显式标题，拒绝修改原文证据。"
    if actual != expected_title:
        return "标题不匹配，拒绝修改原文证据。"
    return ""


async def _preflight_access(bucket_mgr, bucket_id: str, expected_title: str) -> tuple[Any | None, str]:
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return None, f"未找到桶 {bucket_id}。"
    return bucket, _access_error(bucket, expected_title)


def _format_ranges(ranges: list[list[int]]) -> str:
    return ",".join(f"{start}-{end}" for start, end in ranges)


async def attach(
    bucket_mgr,
    source_store,
    bucket_id: str,
    expected_title: str,
    source_content: str,
    source_ranges: Any = None,
) -> str:
    bucket_id = str(bucket_id or "").strip()
    expected_title = _normalized_title(expected_title)
    text = "" if source_content is None else str(source_content)
    if not bucket_id or not expected_title:
        return "source_attach 需要 bucket_id 和 expected_title。"
    if not text.strip():
        return "source_content 必须非空。"
    try:
        ranges = normalize_source_ranges(source_ranges)
        line_count = len(text.splitlines()) or 1
        if not ranges:
            ranges = [[1, line_count]]
        elif any(end > line_count for _start, end in ranges):
            return f"source_ranges 超出原文总行数 {line_count}。"
        bucket, access_error = await _preflight_access(bucket_mgr, bucket_id, expected_title)
        if access_error:
            return access_error
        candidate_ref = "src_" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        candidate_key = (candidate_ref, tuple(tuple(pair) for pair in ranges))
        preflight_links = source_links_from_metadata((bucket or {}).get("metadata") or {})
        already_bound = any(_key(link) == candidate_key for link in preflight_links)
        if not already_bound and len(preflight_links) >= MAX_SOURCE_LINKS:
            return f"source_attach 拒绝：source_links 上限 {MAX_SOURCE_LINKS}。"
        if not already_bound and len(active_source_refs_from_links(preflight_links)) >= MAX_SOURCE_REFS:
            return f"source_attach 拒绝：活动 source_refs 上限 {MAX_SOURCE_REFS}。"
        ref = source_store.put(text)
    except (OSError, UnicodeError, ValueError) as exc:
        return f"source_attach 失败：{exc}"
    candidate = {"ref": ref, "ranges": ranges, "status": "active"}

    def mutation(post: Any) -> tuple[bool, str]:
        error = _access_error(post, expected_title)
        if error:
            return False, error
        try:
            links = source_links_from_metadata(post.metadata)
        except ValueError:
            return False, "该桶的原文证据引用格式无效，拒绝修改。"
        key = _key(candidate)
        for index, link in enumerate(links, 1):
            if _key(link) == key:
                if link["status"] == "active":
                    return False, (
                        f"source_attach ok bucket_id={bucket_id} slot={index} "
                        f"status=active ranges={_format_ranges(ranges)}"
                    )
                return False, f"source_attach detached bucket_id={bucket_id} slot={index}; 请使用 source_restore。"
        if len(links) >= MAX_SOURCE_LINKS:
            return False, f"source_attach 拒绝：source_links 上限 {MAX_SOURCE_LINKS}。"
        if len(active_source_refs_from_links(links)) >= MAX_SOURCE_REFS:
            return False, f"source_attach 拒绝：活动 source_refs 上限 {MAX_SOURCE_REFS}。"
        links.append(candidate)
        post["source_links"] = links
        post["source_refs"] = active_source_refs_from_links(links)
        return True, (
            f"source_attach ok bucket_id={bucket_id} slot={len(links)} "
            f"status=active ranges={_format_ranges(ranges)}"
        )

    result = await bucket_mgr.mutate_source_links(bucket_id, mutation)
    return result or f"未找到桶 {bucket_id}。"


async def _change_status(
    bucket_mgr,
    bucket_id: str,
    expected_title: str,
    source_slot: int,
    target: str,
) -> str:
    bucket_id = str(bucket_id or "").strip()
    expected_title = _normalized_title(expected_title)
    action = "restore" if target == "active" else "detach"
    if not bucket_id or not expected_title:
        return f"source_{action} 需要 bucket_id 和 expected_title。"
    if isinstance(source_slot, bool) or not isinstance(source_slot, int) or source_slot < 1:
        return "source_slot 必须是从 1 开始的整数。"

    def mutation(post: Any) -> tuple[bool, str]:
        error = _access_error(post, expected_title)
        if error:
            return False, error
        try:
            links = source_links_from_metadata(post.metadata)
        except ValueError:
            return False, "该桶的原文证据引用格式无效，拒绝修改。"
        if source_slot > len(links):
            return False, f"source_slot={source_slot} 不存在。"
        link = links[source_slot - 1]
        if link["status"] == target:
            return False, f"source_{action} ok bucket_id={bucket_id} slot={source_slot} status={target}"
        if target == "active" and len(active_source_refs_from_links(links)) >= MAX_SOURCE_REFS:
            return False, f"source_restore 拒绝：活动 source_refs 上限 {MAX_SOURCE_REFS}。"
        link["status"] = target
        post["source_links"] = links
        post["source_refs"] = active_source_refs_from_links(links)
        return True, f"source_{action} ok bucket_id={bucket_id} slot={source_slot} status={target}"

    result = await bucket_mgr.mutate_source_links(bucket_id, mutation)
    return result or f"未找到桶 {bucket_id}。"


async def detach(
    bucket_mgr,
    bucket_id: str,
    expected_title: str,
    source_slot: int,
) -> str:
    return await _change_status(bucket_mgr, bucket_id, expected_title, source_slot, "detached")


async def restore(
    bucket_mgr,
    bucket_id: str,
    expected_title: str,
    source_slot: int,
) -> str:
    return await _change_status(bucket_mgr, bucket_id, expected_title, source_slot, "active")
