"""Read a compact local Relation ledger without exposing target body text."""

from __future__ import annotations

from relation_bindings import normalized_title, ordinary
from relation_store import normalize_relation_links


async def dispatch(
    bucket_mgr,
    bucket_id: str,
    expected_title: str = "",
    include_titles: bool = False,
    include_detached: bool = False,
) -> str:
    """Read a compact local Relation ledger, expanding target titles only on demand."""
    bucket_id = str(bucket_id or "").strip()
    expected_title = normalized_title(expected_title)
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到桶 {bucket_id}。"
    meta = bucket.get("metadata") or {}
    if not ordinary(bucket, bucket_id):
        return "Relation 只允许普通记忆桶。"
    if expected_title and normalized_title(meta.get("title")) != expected_title:
        return "标题不匹配，拒绝读取 Relation。"
    try:
        links = normalize_relation_links(meta.get("relation_links"))
    except ValueError:
        return "该桶的 relation_links 格式无效，拒绝读取。"

    active_count = sum(link["status"] == "active" for link in links)
    detached_count = len(links) - active_count
    header = f"relation manifest | active={active_count} | detached={detached_count}"
    if not links:
        return header + "\n没有关系。"

    visible = [
        (slot, link)
        for slot, link in enumerate(links, 1)
        if include_detached or link["status"] == "active"
    ]
    if not visible:
        return header + "\n没有 active relation；如需历史关系请设置 include_detached=true。"

    rows = []
    for slot, link in visible:
        parts = [
            f"slot={slot}",
            f"type={link['type']}",
            f"target_bucket_id={link['target_bucket_id']}",
            link["status"],
        ]
        # 新 custom 必有 label；旧 V1 的固定类型若曾写自定义 label，也继续展示。
        if link.get("label"):
            parts.insert(2, f"label={link['label']}")
        if include_titles:
            target = await bucket_mgr.get(link["target_bucket_id"])
            if target and ordinary(target, link["target_bucket_id"]):
                target_title = normalized_title((target.get("metadata") or {}).get("title"))
                if target_title:
                    parts.insert(-1, f"title={target_title}")
        rows.append(" | ".join(parts))
    return header + "\n" + "\n".join(rows)
