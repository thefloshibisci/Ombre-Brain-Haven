"""Validation and presentation helpers for Relation sidecar ledgers."""
from __future__ import annotations

from typing import Any


MAX_RELATION_LINKS = 64
MAX_ACTIVE_RELATION_LINKS = 16
MAX_RELATION_LABEL_CHARS = 20
MAX_RELATION_TYPE_CHARS = 32
MAX_RELATION_ID_CHARS = 64
_FIXED_RELATION_TYPES = frozenset(
    {
        "caused_by",
        "causes",
        "continuation_of",
        "continues",
        "related_to",
        "same_event",
    }
)
_RELATION_TYPES = _FIXED_RELATION_TYPES | {"custom"}
_REVERSE_RELATION_TYPES = {
    "caused_by": "causes",
    "causes": "caused_by",
    "continuation_of": "continues",
    "continues": "continuation_of",
    "related_to": "related_to",
    "same_event": "same_event",
    "custom": "custom",
}
_DEFAULT_DISPLAY_LABELS = {
    "caused_by": "原因",
    "causes": "结果",
    "continuation_of": "前段",
    "continues": "后续",
    "related_to": "相关",
    "same_event": "同一事件",
}


def normalize_relation_type(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("relation_type 必须是字符串安全键")
    value = value.strip().lower()
    if value not in _RELATION_TYPES:
        raise ValueError("relation_type must be one of the six fixed types or custom")
    return value


def reverse_relation_type(value: Any) -> str:
    return _REVERSE_RELATION_TYPES[normalize_relation_type(value)]


def is_fixed_relation_type(value: Any) -> bool:
    return normalize_relation_type(value) in _FIXED_RELATION_TYPES


def normalize_relation_label(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("relation label 必须是字符串")
    if "\r" in value or "\n" in value:
        raise ValueError("relation label 不允许换行")
    value = value.strip()
    if len(value) > MAX_RELATION_LABEL_CHARS:
        raise ValueError(f"relation label 最多 {MAX_RELATION_LABEL_CHARS} 个字符")
    return value


def normalize_relation_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError("relation_id 必须是字符串")
    value = value.strip()
    if not value or "\r" in value or "\n" in value or len(value) > MAX_RELATION_ID_CHARS:
        raise ValueError("relation_id 格式无效")
    return value


def normalize_relation_links(value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("relation_links 必须是列表")
    if len(value) > MAX_RELATION_LINKS:
        raise ValueError(f"relation_links 过多（{len(value)} > {MAX_RELATION_LINKS}）")
    links: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("relation_links 每项必须是对象")
        target_bucket_id = item.get("target_bucket_id")
        if not isinstance(target_bucket_id, str):
            raise ValueError("relation_links target_bucket_id 必须是字符串")
        target_bucket_id = target_bucket_id.strip()
        if not target_bucket_id or "\r" in target_bucket_id or "\n" in target_bucket_id:
            raise ValueError("relation_links 包含非法 target_bucket_id")
        status = item.get("status")
        if not isinstance(status, str):
            raise ValueError("relation_links status 必须是字符串")
        status = status.strip().lower()
        if status not in {"active", "detached"}:
            raise ValueError("relation_links status 必须是 active 或 detached")
        relation_type = normalize_relation_type(item.get("type"))
        label = normalize_relation_label(item.get("label"))
        if relation_type == "custom" and not label:
            raise ValueError("custom relation 必须有 label")
        relation_id = normalize_relation_id(item.get("relation_id"))
        normalized: dict[str, str] = {
            "target_bucket_id": target_bucket_id,
            "type": relation_type,
            "label": label,
            "status": status,
        }
        # V1 历史单向边没有 relation_id，保留原形不强制迁移。
        if relation_id:
            normalized["relation_id"] = relation_id
        links.append(normalized)
    if sum(item["status"] == "active" for item in links) > MAX_ACTIVE_RELATION_LINKS:
        raise ValueError(f"活动 relation_links 过多（>{MAX_ACTIVE_RELATION_LINKS}）")
    return links


def relation_display_label(relation_type: str, label: str | None = "") -> str:
    """Render a short human-facing label without reading the target bucket."""
    relation_type = normalize_relation_type(relation_type)
    label = normalize_relation_label(label)
    if relation_type == "custom":
        return label or "自定义"
    base = _DEFAULT_DISPLAY_LABELS.get(relation_type, relation_type)
    # 新建的固定六型不写 label；旧 V1 若已存在 label，仍保留展示以免丢信息。
    return f"{base}·{label}" if label else base


def relation_hint(bucket: dict, limit: int = 2) -> str:
    meta = bucket.get("metadata") or {}
    if str(meta.get("type") or "dynamic").strip().lower() in {
        "plan",
        "feel",
        "letter",
        "i",
        "i_candidate",
        "identity",
    }:
        return ""
    try:
        links = normalize_relation_links(meta.get("relation_links"))
    except ValueError:
        return ""
    active = [link for link in links if link["status"] == "active"]
    rows = []
    for link in active[:limit]:
        label = relation_display_label(link["type"], link["label"])
        rows.append(f"↳ {label} → {link['target_bucket_id']}")
    hidden = len(active) - len(rows)
    if hidden > 0:
        rows.append(f"↳ 另有 {hidden} 条 relation")
    return "\n".join(rows)
