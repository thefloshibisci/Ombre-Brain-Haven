"""Reversible, bidirectional bucket-to-bucket Relation operations."""

from __future__ import annotations

import secrets
from typing import Any

from relation_store import (
    MAX_ACTIVE_RELATION_LINKS,
    MAX_RELATION_LINKS,
    is_fixed_relation_type,
    normalize_relation_label,
    normalize_relation_links,
    normalize_relation_type,
    reverse_relation_type,
)


def normalized_title(value: object) -> str:
    """Normalize a title without applying a hard cap (Haven has no title limit)."""
    import unicodedata

    return " ".join(unicodedata.normalize("NFC", str(value or "")).split())


def ordinary(bucket: Any, bucket_id: str = "") -> bool:
    """Haven keeps only ``feel`` out of relation; archived buckets remain ordinary."""
    meta = (bucket.get("metadata") if isinstance(bucket, dict) else getattr(bucket, "metadata", {})) or {}
    kind = str(meta.get("type") or "dynamic").strip().lower()
    return kind not in {"feel"}


def _access(post: Any, bucket_id: str, expected_title: str = "") -> str:
    meta = getattr(post, "metadata", {}) or {}
    if not ordinary({"metadata": meta}, bucket_id):
        return "Relation 只允许普通记忆桶。"
    if expected_title:
        actual = normalized_title(meta.get("title"))
        if not actual or actual != expected_title:
            return "标题不匹配，拒绝修改 Relation。"
    return ""


def _relation_key(link: dict[str, str]) -> tuple[str, str, str]:
    return (link["target_bucket_id"], link["type"], link["label"])


def _capacity_error(links: list[dict[str, str]], *, restoring: bool = False) -> str:
    if len(links) >= MAX_RELATION_LINKS and not restoring:
        return f"relation_links 上限 {MAX_RELATION_LINKS}。"
    if sum(link["status"] == "active" for link in links) >= MAX_ACTIVE_RELATION_LINKS:
        prefix = "relation_restore" if restoring else "relation_attach"
        return f"{prefix} 拒绝：活动 relation_links 上限 {MAX_ACTIVE_RELATION_LINKS}。"
    return ""


async def attach(
    bucket_mgr,
    bucket_id: str,
    target_bucket_id: str,
    relation_type: str,
    expected_title: str = "",
    label: str = "",
    reverse_label: str = "",
) -> str:
    """Create one logical relation and persist mirrored local views on both buckets."""
    if not isinstance(target_bucket_id, str):
        return "relation_attach 拒绝：target_bucket_id 必须是字符串。"
    bucket_id = str(bucket_id or "").strip()
    target_bucket_id = str(target_bucket_id or "").strip()
    expected_title = normalized_title(expected_title)
    if not bucket_id or not target_bucket_id:
        return "relation_attach 需要 bucket_id 与 target_bucket_id。"
    if bucket_id == target_bucket_id:
        return "relation_attach 拒绝：不允许同桶自环。"

    try:
        relation_type = normalize_relation_type(relation_type)
        label = normalize_relation_label(label)
        reverse_label = normalize_relation_label(reverse_label)
    except ValueError as exc:
        return f"relation_attach 拒绝：{exc}"

    if is_fixed_relation_type(relation_type):
        if label or reverse_label:
            return (
                "relation_attach 拒绝：固定六种 relation_type 自动反向，"
                "不使用 label/reverse_label；自定义关系请使用 custom。"
            )
        forward_label = ""
        backward_label = ""
    else:
        if not label:
            return "relation_attach 拒绝：custom relation 必须填 label。"
        forward_label = label
        backward_label = reverse_label or label

    reverse_type = reverse_relation_type(relation_type)
    source = await bucket_mgr.get(bucket_id)
    target = await bucket_mgr.get(target_bucket_id)
    if source and target and (not ordinary(source, bucket_id) or not ordinary(target, target_bucket_id)):
        return "Relation 只允许普通记忆桶。"
    if not source or not target:
        return "relation_attach 拒绝：source 与 target 都必须真实存在。"

    relation_id = f"rel_{secrets.token_hex(6)}"
    candidate = {
        "relation_id": relation_id,
        "target_bucket_id": target_bucket_id,
        "type": relation_type,
        "label": forward_label,
        "status": "active",
    }
    mirror = {
        "relation_id": relation_id,
        "target_bucket_id": bucket_id,
        "type": reverse_type,
        "label": backward_label,
        "status": "active",
    }

    def mutation(source_post: Any, target_post: Any):
        error = _access(source_post, bucket_id, expected_title)
        if error:
            return False, False, error
        if not ordinary({"metadata": getattr(target_post, "metadata", {}) or {}}, target_bucket_id):
            return False, False, "Relation 只允许普通记忆桶。"
        try:
            source_links = normalize_relation_links(source_post.metadata.get("relation_links"))
            target_links = normalize_relation_links(target_post.metadata.get("relation_links"))
        except ValueError:
            return False, False, "任一端的 relation_links 格式无效，拒绝修改。"

        key = (target_bucket_id, relation_type, forward_label)
        for slot, link in enumerate(source_links, 1):
            if _relation_key(link) == key:
                status_text = "ok" if link["status"] == "active" else "detached"
                restore_hint = "；请使用 relation_restore。" if link["status"] == "detached" else ""
                return (
                    False,
                    False,
                    f"relation_attach {status_text} bucket_id={bucket_id} slot={slot}" + restore_hint,
                )

        source_capacity = _capacity_error(source_links)
        if source_capacity:
            return False, False, f"relation_attach 拒绝：source {source_capacity}"
        target_capacity = _capacity_error(target_links)
        if target_capacity:
            return False, False, f"relation_attach 拒绝：target {target_capacity}"

        source_links.append(candidate)
        target_links.append(mirror)
        source_post["relation_links"] = source_links
        target_post["relation_links"] = target_links
        return (
            True,
            True,
            f"relation_attach ok bucket_id={bucket_id} slot={len(source_links)} "
            f"target_bucket_id={target_bucket_id} target_slot={len(target_links)} "
            f"relation_id={relation_id} status=active",
        )

    result = await bucket_mgr.mutate_relation_pair(bucket_id, target_bucket_id, mutation)
    return result or f"未找到桶 {bucket_id} 或 {target_bucket_id}。"


async def _change_legacy(
    bucket_mgr,
    bucket_id: str,
    expected_title: str,
    relation_slot: int,
    status: str,
) -> str:
    action = "restore" if status == "active" else "detach"

    def mutation(post: Any):
        error = _access(post, bucket_id, expected_title)
        if error:
            return False, error
        try:
            links = normalize_relation_links(post.metadata.get("relation_links"))
        except ValueError:
            return False, "该桶的 relation_links 格式无效，拒绝修改。"
        if relation_slot > len(links):
            return False, f"relation_slot={relation_slot} 不存在。"
        link = links[relation_slot - 1]
        if link["status"] == status:
            return False, (
                f"relation_{action} ok bucket_id={bucket_id} slot={relation_slot} "
                f"status={status} legacy=true"
            )
        if status == "active":
            capacity = _capacity_error(links, restoring=True)
            if capacity:
                return False, capacity
        link["status"] = status
        post["relation_links"] = links
        return True, (
            f"relation_{action} ok bucket_id={bucket_id} slot={relation_slot} "
            f"status={status} legacy=true"
        )

    result = await bucket_mgr.mutate_relation_links(bucket_id, mutation)
    return result or f"未找到桶 {bucket_id}。"


async def _change(
    bucket_mgr,
    bucket_id: str,
    relation_slot: int,
    status: str,
    expected_title: str = "",
) -> str:
    action = "restore" if status == "active" else "detach"
    bucket_id = str(bucket_id or "").strip()
    expected_title = normalized_title(expected_title)
    if isinstance(relation_slot, bool) or not isinstance(relation_slot, int) or relation_slot < 1:
        return "relation_slot 必须是从 1 开始的整数。"

    source = await bucket_mgr.get(bucket_id)
    if not source:
        return f"未找到桶 {bucket_id}。"
    if not ordinary(source, bucket_id):
        return "Relation 只允许普通记忆桶。"
    try:
        source_links = normalize_relation_links((source.get("metadata") or {}).get("relation_links"))
    except ValueError:
        return "该桶的 relation_links 格式无效，拒绝修改。"
    if relation_slot > len(source_links):
        return f"relation_slot={relation_slot} 不存在。"

    seed_link = source_links[relation_slot - 1]
    relation_id = str(seed_link.get("relation_id") or "").strip()
    if not relation_id:
        # V1 历史单向边没有镜像 ID，原地保持旧行为。
        return await _change_legacy(bucket_mgr, bucket_id, expected_title, relation_slot, status)

    target_bucket_id = seed_link["target_bucket_id"]

    def mutation(source_post: Any, target_post: Any):
        error = _access(source_post, bucket_id, expected_title)
        if error:
            return False, False, error
        if not ordinary({"metadata": getattr(target_post, "metadata", {}) or {}}, target_bucket_id):
            return False, False, "Relation 只允许普通记忆桶。"
        try:
            left = normalize_relation_links(source_post.metadata.get("relation_links"))
            right = normalize_relation_links(target_post.metadata.get("relation_links"))
        except ValueError:
            return False, False, "任一端的 relation_links 格式无效，拒绝修改。"
        if relation_slot > len(left):
            return False, False, f"relation_slot={relation_slot} 不存在。"
        source_link = left[relation_slot - 1]
        if str(source_link.get("relation_id") or "") != relation_id:
            return False, False, "relation_slot 已变化，请重新 relation_read 后再试。"

        mirror_slot = next(
            (i for i, item in enumerate(right) if str(item.get("relation_id") or "") == relation_id),
            None,
        )
        if mirror_slot is None:
            return False, False, "双向 Relation 镜像缺失，拒绝只修改单边。"
        target_link = right[mirror_slot]

        if status == "active":
            if source_link["status"] != "active":
                capacity = _capacity_error(left, restoring=True)
                if capacity:
                    return False, False, f"source {capacity}"
            if target_link["status"] != "active":
                capacity = _capacity_error(right, restoring=True)
                if capacity:
                    return False, False, f"target {capacity}"

        left_changed = source_link["status"] != status
        right_changed = target_link["status"] != status
        source_link["status"] = status
        target_link["status"] = status
        source_post["relation_links"] = left
        target_post["relation_links"] = right
        return (
            left_changed,
            right_changed,
            f"relation_{action} ok bucket_id={bucket_id} slot={relation_slot} "
            f"relation_id={relation_id} status={status}",
        )

    result = await bucket_mgr.mutate_relation_pair(bucket_id, target_bucket_id, mutation)
    return result or f"未找到桶 {bucket_id} 或 {target_bucket_id}。"


async def detach(bucket_mgr, bucket_id: str, relation_slot: int, expected_title: str = "") -> str:
    return await _change(bucket_mgr, bucket_id, relation_slot, "detached", expected_title)


async def restore(bucket_mgr, bucket_id: str, relation_slot: int, expected_title: str = "") -> str:
    return await _change(bucket_mgr, bucket_id, relation_slot, "active", expected_title)
