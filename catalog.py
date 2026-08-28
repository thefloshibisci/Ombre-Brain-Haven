"""Token-efficient read-only catalog over bucket metadata."""

from errors import safe_error_detail
from letter_lock import is_letter_bucket, letter_lock_state
from relation_store import relation_hint
from source_store import normalize_source_refs
from utils import parse_bool


_SECTIONS = [
    ("permanent", "固化"),
    ("dynamic", "动态"),
    ("feel", "feel"),
    ("letter", "letter"),
]


def _source_hint(bucket: dict) -> str:
    meta = bucket.get("metadata") or {}
    try:
        refs = normalize_source_refs(meta.get("source_refs"))
    except (KeyError, TypeError, ValueError):
        return ""
    if not refs:
        return ""
    title = " ".join(str(meta.get("title") or "").split())
    if title:
        return f"[source_available:true | source_title:{title} | use:source_read]"
    return "[source_available:true | source_read requires an explicit title]"


def _importance(bucket: dict) -> int:
    try:
        return int((bucket.get("metadata") or {}).get("importance") or 0)
    except (TypeError, ValueError):
        return 0


async def surface_catalog(
    bucket_manager,
    domain_filter: list[str] | None = None,
    tag_filter: list[str] | None = None,
    max_results: int = 20,
) -> str:
    """Return metadata-only rows grouped by logical memory section."""
    try:
        buckets = await bucket_manager.list_all(include_archive=False)
    except Exception as exc:
        return f"获取记忆目录失败: {safe_error_detail(exc)}"

    if not buckets:
        return "记忆库为空。"

    grouped: dict[str, list[tuple[int, str]]] = {key: [] for key, _ in _SECTIONS}
    for bucket in buckets:
        metadata = bucket.get("metadata") or {}
        logical_letter = is_letter_bucket(bucket)
        state = letter_lock_state(bucket, caller_side="ai")
        letter_locked = logical_letter and bool(state.get("locked"))
        domains = (
            ["letter"]
            if letter_locked
            else [item for item in (metadata.get("domain") or []) if item]
        )
        if domain_filter and not any(str(item) in domain_filter for item in domains):
            continue
        bucket_tags = (
            {"__letter__"}
            if letter_locked
            else {str(item) for item in (metadata.get("tags") or [])}
        )
        if tag_filter and not all(str(tag) in bucket_tags for tag in tag_filter):
            continue

        name = metadata.get("name") or bucket.get("id")
        prefix = ""
        if not logical_letter:
            if parse_bool(metadata.get("protected"), default=False):
                prefix = "🛡️ [受保护记忆] "
                if parse_bool(metadata.get("pinned"), default=False):
                    prefix = "📌" + prefix
            elif parse_bool(metadata.get("pinned"), default=False):
                prefix = "📌"
            if parse_bool(metadata.get("anchor"), default=False):
                prefix += "⚓ [anchor] "
        else:
            name = "一封上锁的信"

        row = (
            f"{prefix}{name} | "
            f"{','.join(str(item) for item in domains) or '未分类'} | "
            f"{_importance(bucket)}"
        )
        if not letter_locked:
            source_hint = _source_hint(bucket)
            if source_hint:
                row += f" | {source_hint}"
            hint = relation_hint(bucket)
            if hint:
                row += f" | {hint.replace(chr(10), ' | ')}"
        section = "letter" if logical_letter else str(metadata.get("type") or "")
        if section not in grouped:
            section = "dynamic"
        grouped[section].append((_importance(bucket), row))

    total = sum(len(rows) for rows in grouped.values())
    if total == 0:
        return "没有匹配过滤条件的记忆桶。"

    ranked: list[tuple[int, str, str]] = []
    for section, _label in _SECTIONS:
        ranked.extend(
            (importance, row, section)
            for importance, row in grouped[section]
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    limited: dict[str, list[tuple[int, str]]] = {key: [] for key, _ in _SECTIONS}
    for item in ranked[: max(1, int(max_results or 0))]:
        limited[item[2]].append((item[0], item[1]))

    total = sum(len(rows) for rows in limited.values())
    parts = [
        f"=== 记忆目录（{total} 桶）===",
        "先看目录定位，再 breath_search(query=...) 精准拉取正文。",
    ]
    for section, label in _SECTIONS:
        rows = limited[section]
        if not rows:
            continue
        rows.sort(key=lambda item: item[0], reverse=True)
        parts.append(f"--- {label}（{len(rows)}）---")
        parts.extend(row for _, row in rows)
    return "\n".join(parts)
