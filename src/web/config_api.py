"""
========================================
web/config_api.py — Dashboard 配置 / 环境变量 / API Key 测试 / 模型列表
========================================

- /dashboard：重定向到根
- /api/env-vars：环境变量只读概览
- /api/config (GET/POST)：运行期配置读取 / 热更新（含 embedding 热替换）
- /api/test/dehydration、/api/test/embedding：压缩 / 向量化连通性自检
- /api/models：列目标 provider 可用模型
- /api/env-config (GET/POST)：四块 env（compress/embed/webhook/password）热更新；
  embedding 改动会原子替换所有 Web/MCP/写入/迁移运行时引用。
  webhook 不再回写模块全局——_fire_webhook 每次读 os.environ。

对外暴露：register(mcp)。
========================================
"""

import asyncio
import copy
import errno
import math
import os
import re
import secrets
import sys
import tempfile
import threading
from collections.abc import Mapping

import httpx
import yaml

from starlette.requests import Request
from starlette.responses import Response

from ombrebrain.security.deployment_profile import (
    assess_mcp_network_safety,
    current_mcp_network_security,
    mcp_network_safety_issue,
    normalize_public_https_origin,
)
from ombrebrain.security.public_origin import configured_public_origin

from . import _shared as sh

try:
    from dehydrator import chat_completion_token_limit
except ImportError:  # pragma: no cover
    from ..dehydrator import chat_completion_token_limit

try:
    from utils import (  # type: ignore
        get_ai_name as _get_ai_name,
        get_owner_name as _get_owner_name,
        get_owner_count as _get_owner_count,
        get_timezone_name as _get_timezone_name,
        positive_float as _positive_float,
        parse_bool as _parse_bool,
        atomic_update_config_yaml,
        read_config_yaml,
    )
except ImportError:  # pragma: no cover
    from ..utils import (  # type: ignore
        get_ai_name as _get_ai_name,
        get_owner_name as _get_owner_name,
        get_owner_count as _get_owner_count,
        get_timezone_name as _get_timezone_name,
        positive_float as _positive_float,
        parse_bool as _parse_bool,
        atomic_update_config_yaml,
        read_config_yaml,
    )

logger = sh.logger
_MAX_PROVIDER_KEY_CHARS = 8192
_MAX_PROVIDER_URL_CHARS = 2048
_MAX_PROVIDER_FORMAT_CHARS = 64
_MAX_ENV_VALUE_CHARS = 8192

_MODEL_API_KEY_ENV = {
    "dehydration": "OMBRE_COMPRESS_API_KEY",
    "embedding": "OMBRE_EMBED_API_KEY",
}
_SECRET_CONFIG_KEY_NAMES = frozenset(
    {
        "api_key",
        "api_keys",
        "api_key_values",
        "access_token",
        "refresh_token",
        "client_secret",
        "password",
        "secret",
        "secrets",
        "token",
        "credential",
        "credentials",
        "authorization",
        "private_key",
    }
)
_MASKED_SECRET_RE = re.compile(r"^[^\s.]{1,8}\.\.\.[^\s.]{1,8}$")


class _CrossLoopAsyncLock:
    """Async mutex that remains safe when routes run on different event loops."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    async def __aenter__(self):
        while not self._lock.acquire(blocking=False):
            await asyncio.sleep(0.005)
        return self

    async def __aexit__(self, _exc_type, _exc, _tb) -> None:
        self._lock.release()


def _bounded_config_int(value, field: str, low: int, high: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer in [{low},{high}]")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"{field} must be an integer in [{low},{high}]"
        ) from exc
    if isinstance(value, float) and (
        not math.isfinite(value) or value != parsed
    ):
        raise ValueError(f"{field} must be an integer in [{low},{high}]")
    if not low <= parsed <= high:
        raise ValueError(f"{field} must be in [{low},{high}]")
    return parsed


def _bounded_config_float(value, field: str, low: float, high: float) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number in [{low},{high}]")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number in [{low},{high}]") from exc
    if not math.isfinite(parsed) or not low <= parsed <= high:
        raise ValueError(f"{field} must be a finite number in [{low},{high}]")
    return parsed



_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXTENDED_CONFIG_SECTIONS = (
    "gateway", "recall", "memory_diffusion", "reranker", "persona",
    "dream", "reflection", "portrait", "self_anchor",
)
_SECRET_ENV_BY_SECTION = {
    "reranker": "OMBRE_RERANKER_API_KEY",
    "persona": "OMBRE_PERSONA_API_KEY",
    "dream": "OMBRE_DREAM_API_KEY",
    "reflection": "OMBRE_REFLECTION_API_KEY",
    "portrait": "OMBRE_PORTRAIT_API_KEY",
}
_GATEWAY_SECRET_ENV = "OMBRE_DOMAIN_SENTINEL_API_KEY"

_GATEWAY_BOOL_FIELDS = {
    "semantic_session_dedupe_enabled", "just_now_context_enabled",
    "memory_sentinel_enabled", "domain_sentinel_enabled",
    "domain_sentinel_enable_thinking", "date_persona_trace_enabled",
    "date_persona_trace_include_daily", "date_recall_enabled",
    "operit_context_rewrite_enabled", "word_map_hint_enabled",
    "portrait_memory_enabled", "portrait_memory_include_anchors",
    "query_planner_enabled", "semantic_rescue_enabled",
    "memory_detail_recall_enabled",
}
_GATEWAY_INT_FIELDS = {
    "skip_recent_rounds": (0, 10000),
    "recent_context_budget": (0, 200000),
    "just_now_context_max_turns": (0, 10000),
    "just_now_context_budget": (0, 200000),
    "conversation_turns_max_entries": (0, 100000),
    "domain_sentinel_max_tokens": (1, 100000),
    "date_persona_trace_budget": (0, 200000),
    "date_persona_trace_max_events": (0, 10000),
    "date_recall_budget": (0, 200000),
    "date_recall_max_turns": (0, 10000),
    "date_recall_max_buckets": (0, 1000),
    "recalled_memory_budget": (0, 200000),
    "related_memory_budget": (0, 200000),
    "semantic_candidate_top_k": (1, 1000),
    "moment_search_limit": (1, 1000),
    "diffusion_inject_max_items": (0, 100),
    "diffusion_explore_multiplier": (1, 100),
    "current_inner_state_interval_rounds": (0, 10000),
    "portrait_memory_budget": (0, 200000),
    "portrait_memory_max_sources": (0, 1000),
    "query_planner_min_chars": (0, 100000),
    "query_planner_max_queries": (1, 100),
    "query_planner_max_tokens": (1, 100000),
    "semantic_rescue_candidate_limit": (1, 1000),
    "semantic_rescue_max_tokens": (1, 100000),
    "memory_detail_recall_max_ids": (1, 100),
    "memory_detail_recall_budget": (0, 200000),
}
_GATEWAY_FLOAT_FIELDS = {
    "cooldown_hours": (0.0, 87600.0),
    "recent_context_cooldown_hours": (0.0, 87600.0),
    "recent_context_reentry_idle_hours": (0.0, 87600.0),
    "semantic_session_dedupe_threshold": (0.0, 1.0),
    "semantic_session_dedupe_lexical_threshold": (0.0, 1.0),
    "just_now_context_hours": (0.0, 87600.0),
    "domain_sentinel_timeout_seconds": (0.1, 600.0),
    "bucket_list_cache_ttl_seconds": (0.0, 86400.0),
    "diffusion_inject_min_confidence": (0.0, 1.0),
    "semantic_rescue_timeout_seconds": (0.1, 600.0),
}
_GATEWAY_STRING_FIELDS = {
    "query_planner_model", "domain_sentinel_model", "domain_sentinel_base_url",
}
_GATEWAY_CHOICES = {
    "direct_render_mode": {"auto", "compact", "full"},
    "retrieval_mode": {"graph", "bucket"},
    "recall_fusion_mode": {"dynamic", "legacy"},
}

_SECTION_RULES = {
    "recall": {"query_resurface_enabled": ("bool",)},
    "memory_diffusion": {
        "enabled": ("bool",), "max_hops": ("int", 1, 8),
        "top_k": ("int", 0, 20), "min_activation": ("float", 0, 10),
        "max_paths_per_hit": ("int", 1, 10), "chain_walk_enabled": ("bool",),
        "chain_max_hops": ("int", 1, 12), "chain_min_strength": ("float", 0, 10),
        "chain_min_confidence": ("float", 0, 1),
        "chain_min_relation_priority": ("int", 0, 100),
        "chain_max_frontier": ("int", 1, 200),
    },
    "reranker": {
        "enabled": ("bool",), "model": ("str",), "base_url": ("str",),
        "timeout_seconds": ("float", 1, 120), "candidate_limit": ("int", 1, 100),
        "score_weight": ("float", 0, 1),
    },
    "persona": {
        "enabled": ("bool",), "event_recording_enabled": ("bool",),
        "conflict_nudge_enabled": ("bool",), "model": ("str",), "base_url": ("str",),
    },
    "dream": {
        "enabled": ("bool",), "auto_enabled": ("bool",), "surface_enabled": ("bool",),
        "inject_enabled": ("bool",), "retain_after_inject": ("bool",),
        "raw_residue_enabled": ("bool",), "model": ("str",), "base_url": ("str",),
        "identity_anchor_id": ("str",), "thinking_mode": ("str",),
        "temperature": ("float", 0, 2), "max_tokens": ("int", 1, 100000),
        "daily_hour": ("int", 0, 23), "run_window_hours": ("int", 1, 24),
        "daily_probability": ("float", 0, 1), "min_material_count": ("int", 0, 100000),
        "material_window_hours": ("int", 1, 87600), "raw_residue_turns": ("int", 0, 10000),
        "raw_residue_max_chars": ("int", 0, 1000000),
    },
    "reflection": {
        "enabled": ("bool",), "auto_enabled": ("bool",), "daily_enabled": ("bool",),
        "memory_affect_anchor_enabled": ("bool",),
        "relationship_weather_affect_anchor_enabled": ("bool",),
        "daily_activity_summary_enabled": ("bool",), "model": ("str",),
        "base_url": ("str",), "thinking_mode": ("choice", {"", "enabled", "disabled"}),
        "daily_min_memory_items": ("int", 0, 100000),
        "daily_conversation_turn_limit": ("int", 0, 100000),
        "daily_activity_summary_turn_limit": ("int", 0, 100000),
        "daily_activity_summary_max_tokens": ("int", 1, 100000),
        "daily_chat_memory_mode": ("choice", {"auto", "review", "off"}),
        "daily_chat_memory_hour": ("int", 0, 23),
        "daily_chat_memory_turn_limit": ("int", 0, 100000),
        "daily_chat_memory_max_per_day": ("int", 0, 100000),
        "daily_chat_memory_min_confidence": ("float", 0, 1),
        "daily_chat_memory_review_max_per_day": ("int", 0, 100000),
        "daily_chat_memory_review_min_confidence": ("float", 0, 1),
        "daily_chat_memory_summary_enabled": ("bool",),
        "daily_chat_memory_summary_window_turns": ("int", 1, 200),
        "daily_chat_memory_summary_stride_turns": ("int", 1, 200),
        "daily_chat_memory_api_key_env": ("str",),
        "daily_chat_memory_base_url": ("str",),
        "daily_chat_memory_timeout_seconds": ("float", 30, 300),
        "daily_chat_memory_summary_model": ("str",),
        "daily_chat_memory_summary_max_tokens": ("int", 300, 4000),
        "daily_chat_memory_candidate_model": ("str",),
        "daily_chat_memory_candidate_max_tokens": ("int", 300, 4000),
    },
    "portrait": {
        "enabled": ("bool",), "auto_enabled": ("bool",),
        "auto_initial_enabled": ("bool",), "daily_enabled": ("bool",),
        "model": ("str",), "base_url": ("str",), "state_path": ("str",),
        "thinking_mode": ("choice", {"", "enabled", "disabled"}),
        "temperature": ("float", 0, 2), "max_tokens": ("int", 1, 100000),
        "daily_hour": ("int", 0, 23), "check_interval_minutes": ("int", 1, 10080),
        "material_limit": ("int", 1, 100000), "first_run_material_limit": ("int", 1, 100000),
        "persona_events_limit": ("int", 0, 100000), "recent_buffer_max": ("int", 0, 100000),
        "staging_pool_max": ("int", 0, 100000), "candidate_max": ("int", 0, 100000),
        "user_rewrite_evidence_delta": ("int", 0, 100000),
        "manual_suppress_days": ("int", 0, 36500),
    },
    "self_anchor": {"entry_bucket_id": ("str",)},
}


def _mask_secret(value: object) -> str:
    secret = str(value or "").strip()
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}...{secret[-4:]}"


def _config_section(config: Mapping[str, object], name: str) -> dict:
    value = config.get(name, {})
    return dict(value) if isinstance(value, Mapping) else {}


def _dashboard_split_names(value) -> list[str]:
    candidates = re.split(r"[\n,]+", value) if isinstance(value, str) else value if isinstance(value, list) else []
    names: list[str] = []
    for candidate in candidates:
        name = str(candidate or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _dashboard_api_key_values(value) -> list[str]:
    candidates = value if isinstance(value, list) else value.splitlines() if isinstance(value, str) else []
    values = [str(item or "").strip() for item in candidates]
    while values and not values[-1]:
        values.pop()
    return values


def _dashboard_is_secret_config_key(name: object) -> bool:
    normalized = str(name or "").strip().lower().replace("-", "_")
    if not normalized:
        return False
    # Environment variable names and already-safe status fields are metadata,
    # rather than credentials.  Keep them visible in the dashboard projection.
    if normalized.endswith(("_env", "_envs", "_masked", "_ready", "_configured")):
        return False
    if normalized in _SECRET_CONFIG_KEY_NAMES:
        return True
    return normalized.endswith(
        ("_api_key", "_api_keys", "_token", "_password", "_secret", "_credential", "_authorization")
    )


def _dashboard_copy_config_value(value, field: str, *, depth: int = 0):
    """Copy JSON-shaped config data while rejecting unclassified secrets."""
    if depth > 8:
        raise ValueError(f"{field} is nested too deeply")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > _MAX_ENV_VALUE_CHARS:
            raise ValueError(f"{field} is too long")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must be finite")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            key_name = str(key)
            if _dashboard_is_secret_config_key(key_name):
                raise ValueError(f"{field}.{key_name} must not contain plaintext secrets")
            result[key_name] = _dashboard_copy_config_value(
                child, f"{field}.{key_name}", depth=depth + 1
            )
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 1000:
            raise ValueError(f"{field} has too many items")
        return [
            _dashboard_copy_config_value(child, f"{field}[{index}]", depth=depth + 1)
            for index, child in enumerate(value)
        ]
    raise ValueError(f"{field} must contain JSON-compatible values")


def _dashboard_redact_config_value(value, *, depth: int = 0):
    """Return a JSON-safe view with every secret-like field removed."""
    if depth > 8:
        return None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            key_name = str(key)
            if _dashboard_is_secret_config_key(key_name):
                continue
            redacted = _dashboard_redact_config_value(child, depth=depth + 1)
            if redacted is not None or child is None:
                result[key_name] = redacted
        return result
    if isinstance(value, (list, tuple)):
        return [
            _dashboard_redact_config_value(child, depth=depth + 1)
            for child in value[:1000]
        ]
    return None


def _dashboard_secret_input(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    secret = value.strip()
    if len(secret) > _MAX_PROVIDER_KEY_CHARS:
        raise ValueError(f"{field} is too long")
    if any(char in secret for char in ("\r", "\n", "\x00")):
        raise ValueError(f"{field} contains invalid control characters")
    if secret == "***" or _MASKED_SECRET_RE.fullmatch(secret):
        raise ValueError(f"{field} must contain the actual key, not a masked value")
    return secret


def _dashboard_read_env_value(name: str) -> str:
    reader = getattr(sh, "_read_env_var", None)
    if callable(reader):
        try:
            return str(reader(name) or "").strip()
        except Exception:
            pass
    return str(os.environ.get(name, "") or "").strip()


def _dashboard_inline_key_count(value) -> int:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list):
        values = value
    else:
        values = []
    count = 0
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("api_key") or item.get("key")
        if str(item or "").strip():
            count += 1
    return count


def _dashboard_sanitize_env_names(value) -> list[str]:
    names = _dashboard_split_names(value)
    for name in names:
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f'invalid api key env name "{name}"')
    return names


def _dashboard_sanitize_upstream_models(raw_models) -> list:
    raw_items = [item.strip() for item in raw_models.split(",")] if isinstance(raw_models, str) else raw_models if isinstance(raw_models, list) else []
    models: list = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, dict):
            public = str(item.get("id") or item.get("alias") or item.get("name") or item.get("model") or item.get("upstream_model") or "").strip()
            upstream = str(item.get("upstream_model") or item.get("provider_model") or item.get("target_model") or item.get("model") or public).strip()
            safe_item = _dashboard_copy_config_value(item, "gateway.upstreams.models[]")
            if not isinstance(safe_item, dict):
                safe_item = {}
            safe_item["id"] = public
            if upstream and upstream != public:
                safe_item["upstream_model"] = upstream
            else:
                safe_item.pop("upstream_model", None)
        else:
            public = str(item or "").strip()
            upstream = public
            safe_item = public
        if not public or public in seen:
            continue
        if len(public) > _MAX_PROVIDER_URL_CHARS or len(upstream) > _MAX_PROVIDER_URL_CHARS:
            raise ValueError("gateway.upstreams.models entries are too long")
        seen.add(public)
        models.append(safe_item)
    return models


def _dashboard_normalize_upstream_protocol(value) -> str:
    return "anthropic" if str(value or "openai").strip().lower() in {"anthropic", "claude"} else "openai"


def _dashboard_copy_existing_upstream(existing: Mapping[str, object]) -> dict:
    """Preserve stored settings; only the GET projection should redact them."""
    result = copy.deepcopy(dict(existing))
    result.pop("api_key_values", None)
    return result


def _dashboard_merge_config_patch(current: dict, patch: Mapping) -> None:
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(current.get(key), dict):
            _dashboard_merge_config_patch(current[key], value)
        else:
            current[key] = copy.deepcopy(value)


def _dashboard_upstream_clear_inline_keys(raw: Mapping[str, object]) -> bool:
    if raw.get("clear_api_key") is True:
        return True
    # An explicitly supplied empty api_keys array is the unambiguous clear
    # operation.  A blank api_key field remains the dashboard's "leave unchanged"
    # value, matching the extended settings form.
    if "api_keys" in raw and isinstance(raw.get("api_keys"), (list, tuple)) and _dashboard_inline_key_count(raw.get("api_keys")) == 0:
        return True
    return False


def _dashboard_sanitize_gateway_upstreams(
    raw_upstreams, current_upstreams=None
) -> list[dict]:
    if not isinstance(raw_upstreams, list):
        raise ValueError("gateway.upstreams must be a list")
    current_items = current_upstreams if isinstance(current_upstreams, list) else []
    current_by_name = {
        str(item.get("name") or "").strip(): item
        for item in current_items
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    }
    result: list[dict] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_upstreams, 1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"gateway.upstreams[{index - 1}] must be an object")
        raw_name = str(raw.get("name") or "").strip()
        name = raw_name or f"upstream-{index}"
        if name in seen:
            raise ValueError(f'duplicate gateway upstream name "{name}"')
        seen.add(name)
        existing = current_by_name.get(name)
        if existing is None and not raw_name and index <= len(current_items):
            candidate = current_items[index - 1]
            existing = candidate if isinstance(candidate, Mapping) and not candidate.get("name") else None
        item = _dashboard_copy_existing_upstream(existing) if existing else {}
        item["name"] = name

        if "protocol" in raw or "api_format" in raw or "type" in raw:
            item["protocol"] = _dashboard_normalize_upstream_protocol(
                raw.get("protocol") or raw.get("api_format") or raw.get("type")
            )
        else:
            item.setdefault("protocol", "openai")
        if "base_url" in raw:
            base_url = str(raw.get("base_url") or "").strip().rstrip("/")
            if len(base_url) > _MAX_PROVIDER_URL_CHARS:
                raise ValueError(f"gateway.upstreams[{index - 1}].base_url is too long")
            item["base_url"] = base_url
        else:
            item.setdefault("base_url", "")

        env_names: list[str] = []
        if "api_key_envs" in raw or "api_key_env" in raw:
            env_value = raw.get("api_key_envs", raw.get("api_key_env", []))
            env_names = _dashboard_sanitize_env_names(env_value)
            item.pop("api_key_env", None)
            item.pop("api_key_envs", None)
            if "api_key_envs" in raw:
                if env_names:
                    item["api_key_envs"] = env_names
            elif env_names:
                item["api_key_env"] = (
                    env_names[0] if len(env_names) == 1 else ",".join(env_names)
                )
        else:
            env_names = _dashboard_sanitize_env_names(
                item.get("api_key_envs", item.get("api_key_env", []))
            )

        handled = {
            "name", "protocol", "api_format", "type", "base_url",
            "api_key", "api_keys", "api_key_values", "api_key_env", "api_key_envs",
            "clear_api_key",
            "models", "default_model", "prompt_cache", "prompt_cache_retention",
            "anthropic_version", "anthropic_beta",
        }
        for key in (
            "default_model", "prompt_cache", "prompt_cache_retention",
            "anthropic_version", "anthropic_beta",
        ):
            if key in raw:
                value = str(raw.get(key) or "").strip()
                if len(value) > _MAX_PROVIDER_FORMAT_CHARS:
                    raise ValueError(f"gateway.upstreams[{index - 1}].{key} is too long")
                if value:
                    item[key] = value
                else:
                    item.pop(key, None)
        if "models" in raw:
            models = _dashboard_sanitize_upstream_models(raw.get("models", []))
            item["models"] = models
        for key, value in raw.items():
            key_name = str(key)
            if key_name in handled:
                continue
            if _dashboard_is_secret_config_key(key_name):
                raise ValueError(
                    f"gateway.upstreams[{index - 1}].{key_name} must not contain plaintext secrets"
                )
            _dashboard_merge_config_patch(item, {key_name: _dashboard_copy_config_value(
                value, f"gateway.upstreams[{index - 1}].{key_name}"
            )})

        if "api_key" in raw:
            secret = _dashboard_secret_input(
                raw.get("api_key"), f"gateway.upstreams[{index - 1}].api_key"
            )
            if secret:
                raise ValueError(
                    f"gateway.upstreams[{index - 1}].api_key must use api_key_env and api_key_values"
                )
            if raw.get("clear_api_key") is True:
                item.pop("api_key", None)
                item.pop("api_keys", None)
        if "clear_api_key" in raw and raw.get("clear_api_key") is not True:
            raise ValueError(
                f"gateway.upstreams[{index - 1}].clear_api_key must be true or omitted"
            )
        if _dashboard_upstream_clear_inline_keys(raw):
            item.pop("api_key", None)
            item.pop("api_keys", None)
        if "api_keys" in raw:
            if _dashboard_inline_key_count(raw.get("api_keys")):
                raise ValueError(
                    f"gateway.upstreams[{index - 1}].api_keys must use api_key_envs and api_key_values"
                )
            if _dashboard_upstream_clear_inline_keys(raw):
                item.pop("api_key", None)
                item.pop("api_keys", None)

        values = _dashboard_api_key_values(raw.get("api_key_values", []))
        if values and not env_names:
            raise ValueError(
                f'gateway upstream "{name}" has api key values without matching env names'
            )
        if any(value for value in values):
            # A newly supplied env-backed key supersedes an old inline key.  The
            # actual value is written to .env and sent to the Gateway hot path.
            item.pop("api_key", None)
            item.pop("api_keys", None)
        result.append(item)
    return result


def _dashboard_gateway_upstream_env_updates(raw_upstreams, config_upstreams) -> dict[str, str]:
    updates: dict[str, str] = {}
    for raw, configured in zip(raw_upstreams or [], config_upstreams):
        env_names = _dashboard_sanitize_env_names(configured.get("api_key_envs", configured.get("api_key_env", [])))
        values = _dashboard_api_key_values(raw.get("api_key_values", []))
        if len(values) > len(env_names):
            raise ValueError(f'gateway upstream "{str(raw.get("name") or "upstream").strip()}" has api key values without matching env names')
        for index, value in enumerate(values):
            if not value:
                continue
            secret = _dashboard_secret_input(
                value,
                f'gateway upstream "{str(raw.get("name") or "upstream").strip()}" api_key_values[{index}]',
            )
            env_name = env_names[index]
            previous = updates.get(env_name)
            if previous is not None and previous != secret:
                raise ValueError(f'conflicting values supplied for API key env "{env_name}"')
            updates[env_name] = secret
    return updates


def _dashboard_gateway_hot_upstreams(config_upstreams: list[dict], raw_upstreams, env_updates: dict[str, str]) -> list[dict]:
    result = []
    for item, raw in zip(config_upstreams, raw_upstreams or []):
        hot_item = dict(item)
        env_names = _dashboard_sanitize_env_names(item.get("api_key_envs", item.get("api_key_env", [])))
        if set(env_names).intersection(env_updates):
            # Gateway is a separate process with a stale environment. Send the
            # complete pool and suppress its old env/inline fallback for this reload.
            hot_item["api_key"] = ""
            hot_item["api_keys"] = [
                {"api_key": value, "label": f"env:{name}"}
                for name in env_names
                if (value := env_updates.get(name, _dashboard_read_env_value(name)))
            ]
            hot_item.pop("api_key_env", None)
            hot_item.pop("api_key_envs", None)
        elif _dashboard_upstream_clear_inline_keys(raw):
            hot_item["api_key"] = ""
            hot_item["api_keys"] = []
        result.append(hot_item)
    return result


def _dashboard_gateway_upstreams_payload(gateway_cfg: dict) -> list[dict]:
    payload: list[dict] = []
    raw_upstreams = gateway_cfg.get("upstreams", [])
    if not isinstance(raw_upstreams, list):
        return payload
    for raw in raw_upstreams:
        if not isinstance(raw, dict):
            continue
        env_names = _dashboard_split_names(raw.get("api_key_envs", raw.get("api_key_env", [])))
        direct = int(bool(str(raw.get("api_key") or "").strip()))
        direct += _dashboard_inline_key_count(raw.get("api_keys", []))
        configured_envs = [name for name in env_names if _dashboard_read_env_value(name)]
        key_count = direct + len(configured_envs)
        safe = _dashboard_redact_config_value(raw)
        if not isinstance(safe, dict):
            safe = {}
        safe.update({
            "name": str(raw.get("name") or "").strip(),
            "protocol": _dashboard_normalize_upstream_protocol(raw.get("protocol")),
            "base_url": str(raw.get("base_url") or "").strip(),
            "api_key_envs": env_names,
            "api_key_masked": ["***"] * key_count,
            "key_count": key_count,
            "ready": bool(str(raw.get("base_url") or "").strip() and key_count),
            "default_model": str(raw.get("default_model") or "").strip(),
            "prompt_cache": str(raw.get("prompt_cache") or "").strip(),
            "prompt_cache_retention": str(raw.get("prompt_cache_retention") or "").strip(),
            "anthropic_version": str(raw.get("anthropic_version") or "").strip(),
            "anthropic_beta": str(raw.get("anthropic_beta") or "").strip(),
            "models": _dashboard_sanitize_upstream_models(raw.get("models", [])),
        })
        safe.pop("api_key", None)
        safe.pop("api_keys", None)
        safe.pop("api_key_values", None)
        payload.append(safe)
    return payload


def _sanitize_rule_value(value, field: str, rule: tuple):
    kind = rule[0]
    if kind == "bool":
        return _parse_bool(value)
    if kind == "int":
        return _bounded_config_int(value, field, rule[1], rule[2])
    if kind == "float":
        return _bounded_config_float(value, field, rule[1], rule[2])
    if kind == "choice":
        parsed = str(value or "").strip().lower()
        if parsed not in rule[1]:
            raise ValueError(f"{field} must be one of {sorted(rule[1])}")
        return parsed
    parsed = str(value or "").strip()
    if len(parsed) > _MAX_PROVIDER_URL_CHARS:
        raise ValueError(f"{field} is too long")
    return parsed


def _sanitize_extended_config_payload(body: dict, current_config: Mapping[str, object], persist_env: bool):
    sections: dict[str, dict] = {}
    env_updates: dict[str, str] = {}
    gateway_hot: dict[str, dict] = {}
    restart_sections: list[str] = []
    for name in _EXTENDED_CONFIG_SECTIONS:
        if name in body and not isinstance(body.get(name), dict):
            raise ValueError(f"{name} must be an object")

    if "gateway" in body:
        raw = dict(body["gateway"])
        clean: dict = {}
        for key in _GATEWAY_BOOL_FIELDS:
            if key in raw:
                clean[key] = _parse_bool(raw[key])
        for key, bounds in _GATEWAY_INT_FIELDS.items():
            if key in raw:
                clean[key] = _bounded_config_int(raw[key], f"gateway.{key}", *bounds)
        for key, bounds in _GATEWAY_FLOAT_FIELDS.items():
            if key in raw:
                clean[key] = _bounded_config_float(raw[key], f"gateway.{key}", *bounds)
        for key in _GATEWAY_STRING_FIELDS:
            if key in raw:
                clean[key] = _sanitize_rule_value(raw[key], f"gateway.{key}", ("str",))
        for key, choices in _GATEWAY_CHOICES.items():
            if key in raw:
                clean[key] = _sanitize_rule_value(raw[key], f"gateway.{key}", ("choice", choices))
        raw_upstreams = raw.get("upstreams")
        if "upstreams" in raw:
            current_gateway = _config_section(current_config, "gateway")
            clean["upstreams"] = _dashboard_sanitize_gateway_upstreams(
                raw_upstreams, current_gateway.get("upstreams", [])
            )
            upstream_env = _dashboard_gateway_upstream_env_updates(raw_upstreams, clean["upstreams"])
            env_updates.update(upstream_env)
        if "domain_sentinel_api_key" in raw:
            secret = _dashboard_secret_input(
                raw["domain_sentinel_api_key"], "gateway.domain_sentinel_api_key"
            )
            if secret:
                env_updates[_GATEWAY_SECRET_ENV] = secret
        handled_gateway = set(_GATEWAY_BOOL_FIELDS)
        handled_gateway.update(_GATEWAY_INT_FIELDS)
        handled_gateway.update(_GATEWAY_FLOAT_FIELDS)
        handled_gateway.update(_GATEWAY_STRING_FIELDS)
        handled_gateway.update(_GATEWAY_CHOICES)
        handled_gateway.update({"upstreams", "domain_sentinel_api_key"})
        for key, value in raw.items():
            if key in handled_gateway:
                continue
            if _dashboard_is_secret_config_key(key):
                raise ValueError(f"gateway.{key} must not contain plaintext secrets")
            clean[key] = _dashboard_copy_config_value(value, f"gateway.{key}")
        sections["gateway"] = clean
        hot = copy.deepcopy(clean)
        if "upstreams" in clean:
            hot["upstreams"] = _dashboard_gateway_hot_upstreams(clean["upstreams"], raw_upstreams, env_updates)
        if _GATEWAY_SECRET_ENV in env_updates:
            hot["domain_sentinel_api_key"] = env_updates[_GATEWAY_SECRET_ENV]
        gateway_hot["gateway"] = hot

    for name, rules in _SECTION_RULES.items():
        if name not in body:
            continue
        raw = body[name]
        clean = {key: _sanitize_rule_value(raw[key], f"{name}.{key}", rule) for key, rule in rules.items() if key in raw}
        if name in _SECRET_ENV_BY_SECTION and "api_key" in raw:
            secret = _dashboard_secret_input(raw["api_key"], f"{name}.api_key")
            if secret:
                env_updates[_SECRET_ENV_BY_SECTION[name]] = secret
        if name in _SECRET_ENV_BY_SECTION and "clear_api_key" in raw:
            if raw["clear_api_key"] is not True:
                raise ValueError(f"{name}.clear_api_key must be true or omitted")
            env_updates[_SECRET_ENV_BY_SECTION[name]] = ""
        for key, value in raw.items():
            if key in rules or key in {"api_key", "clear_api_key"}:
                continue
            if _dashboard_is_secret_config_key(key):
                raise ValueError(f"{name}.{key} must not contain plaintext secrets")
            clean[key] = _dashboard_copy_config_value(value, f"{name}.{key}")
        sections[name] = clean
        if name in {"memory_diffusion", "reranker", "persona", "dream"}:
            hot = copy.deepcopy(clean)
            env_name = _SECRET_ENV_BY_SECTION.get(name)
            if env_name and env_name in env_updates:
                hot["api_key"] = env_updates[env_name]
            gateway_hot[name] = hot
        elif name in {"recall", "reflection", "portrait", "self_anchor"}:
            restart_sections.append(name)

    if env_updates and not persist_env:
        raise ValueError("plaintext API keys require persist_env=true")
    return sections, env_updates, gateway_hot, restart_sections


def _dashboard_collect_model_api_key_updates(
    body: Mapping[str, object], env_updates: dict[str, str], persist_env: bool
) -> None:
    """Route top-level model keys through the same persistent env transaction."""
    for section_name, env_name in _MODEL_API_KEY_ENV.items():
        raw = body.get(section_name)
        if not isinstance(raw, Mapping):
            continue
        if "api_key" in raw:
            secret = _dashboard_secret_input(raw["api_key"], f"{section_name}.api_key")
            if secret:
                env_updates[env_name] = secret
        if "clear_api_key" in raw:
            if raw["clear_api_key"] is not True:
                raise ValueError(f"{section_name}.clear_api_key must be true or omitted")
            env_updates[env_name] = ""
    if env_updates and not persist_env:
        # The caller may have only supplied non-secret extended fields.  Only
        # model keys added here need the explicit env persistence opt-in.
        model_key_names = set(_MODEL_API_KEY_ENV.values())
        if model_key_names.intersection(env_updates):
            raise ValueError("plaintext API keys require persist_env=true")


def _dashboard_inline_secret_clear_required(
    body: Mapping[str, object], current_config: Mapping[str, object]
) -> bool:
    """Detect a clear that would otherwise resurrect from YAML on restart."""
    for section_name in (*_MODEL_API_KEY_ENV, *_SECRET_ENV_BY_SECTION):
        raw = body.get(section_name)
        if not isinstance(raw, Mapping) or raw.get("clear_api_key") is not True:
            continue
        previous = _config_section(current_config, section_name)
        if str(previous.get("api_key") or "").strip():
            return True

    gateway = body.get("gateway")
    if isinstance(gateway, Mapping):
        previous_gateway = _config_section(current_config, "gateway")
        previous_upstreams = previous_gateway.get("upstreams", [])
        previous_by_name = {
            str(item.get("name") or "").strip(): item
            for item in previous_upstreams
            if isinstance(item, Mapping)
        }
        raw_upstreams = gateway.get("upstreams")
        if isinstance(raw_upstreams, list):
            for index, raw in enumerate(raw_upstreams):
                if not isinstance(raw, Mapping) or not _dashboard_upstream_clear_inline_keys(raw):
                    continue
                name = str(raw.get("name") or f"upstream-{index + 1}").strip()
                previous = previous_by_name.get(name)
                if previous is None and not raw.get("name") and index < len(previous_upstreams):
                    candidate = previous_upstreams[index]
                    previous = candidate if isinstance(candidate, Mapping) and not candidate.get("name") else None
                if previous and (
                    str(previous.get("api_key") or "").strip()
                    or _dashboard_inline_key_count(previous.get("api_keys"))
                ):
                    return True
    return False


def _extended_config_get_payload(config: Mapping[str, object]) -> dict:
    result: dict[str, dict] = {}
    raw_gateway = _config_section(config, "gateway")
    gateway = _dashboard_redact_config_value(raw_gateway)
    if not isinstance(gateway, dict):
        gateway = {}
    gateway.pop("domain_sentinel_api_key", None)
    gateway["upstreams"] = _dashboard_gateway_upstreams_payload(raw_gateway)
    gateway_secret = _dashboard_read_env_value(_GATEWAY_SECRET_ENV) or str(
        raw_gateway.get("domain_sentinel_api_key") or ""
    ).strip()
    gateway["domain_sentinel_api_key_masked"] = _mask_secret(gateway_secret)
    gateway["domain_sentinel_api_ready"] = bool(gateway_secret and gateway.get("domain_sentinel_base_url"))
    result["gateway"] = gateway
    for name in _EXTENDED_CONFIG_SECTIONS[1:]:
        raw_section = _config_section(config, name)
        section = _dashboard_redact_config_value(raw_section)
        if not isinstance(section, dict):
            section = {}
        secret_env = _SECRET_ENV_BY_SECTION.get(name)
        if secret_env:
            stored_secret = str(raw_section.get("api_key", "") or "").strip()
            fallback_sections = {
                "reranker": ("embedding", "dehydration"),
                "persona": ("dehydration",),
                "dream": (),
                "reflection": ("embedding", "persona", "dehydration"),
                "portrait": ("dehydration", "reflection", "persona"),
            }.get(name, ())
            candidates = [_dashboard_read_env_value(secret_env), stored_secret]
            for fallback_name in fallback_sections:
                fallback_section = _config_section(config, fallback_name)
                candidates.append(str(fallback_section.get("api_key") or "").strip())
            for fallback_env in (
                "OMBRE_EMBED_API_KEY",
                "OMBRE_EMBEDDING_API_KEY",
                "OMBRE_COMPRESS_API_KEY",
                "OMBRE_API_KEY",
            ):
                if fallback_env != secret_env:
                    candidates.append(_dashboard_read_env_value(fallback_env))
            secret = next((candidate for candidate in candidates if candidate), "")
            section["api_key_masked"] = _mask_secret(secret)
            section["api_ready"] = bool(secret)
        result[name] = section
    return result


async def _hot_update_gateway_config(gateway_payload: dict) -> str | None:
    if not gateway_payload:
        return None
    admin_url = os.environ.get("OMBRE_GATEWAY_ADMIN_URL", "").strip()
    token = os.environ.get("OMBRE_GATEWAY_TOKEN", "").strip()
    if not admin_url or not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.post(admin_url, headers={"Authorization": f"Bearer {token}"}, json=gateway_payload)
        if response.status_code >= 400:
            return f"gateway_hot_reload_failed:{response.status_code}"
        return "gateway_hot_reloaded"
    except Exception as exc:
        logger.warning("Gateway hot config update failed: %s", type(exc).__name__)
        return f"gateway_hot_reload_failed:{type(exc).__name__}"

def _dashboard_env_snapshot() -> tuple[str, bool, bytes | None]:
    path = sh._project_env_path()
    exists = os.path.exists(path)
    if not exists:
        return path, False, None
    # Snapshot failures must abort before mutating runtime/YAML state; silently
    # returning an unusable snapshot would make a later rollback incomplete.
    with open(path, "rb") as handle:
        return path, True, handle.read()


def _dashboard_restore_env(snapshot: tuple[str, bool, bytes | None]) -> None:
    path, existed, content = snapshot
    if existed and content is not None:
        current = None
        if os.path.exists(path):
            with open(path, "rb") as handle:
                current = handle.read()
        if current != content:
            _dashboard_atomic_write_env(path, content, current)
    elif not existed:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _dashboard_encode_env_value(value: str) -> str:
    """Encode one value using the syntax understood by ``env_loader``."""
    if not value or not any(char.isspace() or char in "#\\\"" for char in value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _dashboard_atomic_write_env(path: str, content: bytes, previous: bytes | None) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.tmp.", dir=parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
            temporary = ""
        except OSError as exc:
            # A few container deployments bind-mount the env file itself.  Such
            # an inode cannot be renamed, so retain a rollback copy and update it
            # in place only for that specific filesystem error.
            if exc.errno != errno.EBUSY or not os.path.isfile(path):
                raise
            with open(path, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            with open(path, "rb") as handle:
                written_content = handle.read()
            if written_content != content:
                raise OSError(".env verification failed after write")
        with open(path, "rb") as handle:
            written_content = handle.read()
        if written_content != content:
            raise OSError(".env verification failed after write")
    except Exception:
        # ``os.replace`` leaves the old file untouched on ordinary failures.  The
        # in-place bind-mount fallback needs explicit restoration.
        if previous is not None and os.path.isfile(path):
            try:
                with open(path, "wb") as handle:
                    handle.write(previous)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                pass
        elif previous is None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        raise
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _dashboard_write_env_updates(updates: Mapping[str, str]) -> list[str]:
    if not updates:
        return []
    path = sh._project_env_path()
    existed = os.path.exists(path)
    previous = None
    if existed:
        with open(path, "rb") as handle:
            previous = handle.read()
        text = previous.decode("utf-8")
    else:
        text = ""

    lines = text.splitlines(keepends=True)
    for name, value in updates.items():
        if not _ENV_NAME_RE.fullmatch(str(name)):
            raise ValueError(f'invalid api key env name "{name}"')
        if not isinstance(value, str):
            raise ValueError(f"environment value for {name} must be a string")
        encoded = _dashboard_encode_env_value(value)
        replacement = f"{name}={encoded}\n"
        replaced = False
        for index, line in enumerate(lines):
            match = re.match(
                r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=.*(?:\r?\n)?$",
                line,
            )
            if match and match.group(3) == name:
                prefix = f"{match.group(1)}{match.group(2) or ''}"
                lines[index] = f"{prefix}{replacement}"
                replaced = True
        if not replaced:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            lines.append(replacement)

    content = "".join(lines).encode("utf-8")
    old_environment = {name: os.environ.get(name) for name in updates}
    _dashboard_atomic_write_env(path, content, previous)
    try:
        for name, value in updates.items():
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
    except Exception:
        _dashboard_restore_env((path, existed, previous))
        for name, value in old_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        raise
    return [f"env.{name}" for name in updates]


def _rebuild_embedding_runtime():
    """Rebuild and publish one embedding engine to every runtime holder."""
    try:
        from embedding_engine import EmbeddingEngine  # type: ignore
    except ImportError:  # pragma: no cover
        from ..embedding_engine import EmbeddingEngine  # type: ignore

    engine = EmbeddingEngine(sh.config)
    sh.replace_embedding_engine(engine)
    return engine


def _mcp_auth_mode(config: Mapping[str, object] | object) -> str:
    """规范化一个配置快照中的 MCP 鉴权模式。"""
    raw = (
        str(config.get("mcp_auth_mode", "oauth")).strip().lower()
        if isinstance(config, Mapping)
        else "oauth"
    )
    return raw if raw in ("oauth", "token", "hybrid") else "oauth"


def _current_mcp_token() -> str:
    """Live static MCP token — env wins over config.yaml, same priority as validation."""
    return (
        os.environ.get("OMBRE_MCP_TOKEN", "").strip()
        or str(sh.config.get("mcp_token", "") or "").strip()
    )


def _mask_mcp_token(token: str) -> str | None:
    if not token:
        return None
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def register(mcp) -> None:
    # 该锁只保护本次路由注册实例，不绑定 asyncio 事件循环；这样同一处理器
    # 被测试客户端从多个事件循环调用时，也能串行提交而不会触发跨循环错误。
    mcp_token_commit_lock = threading.Lock()
    config_commit_lock = _CrossLoopAsyncLock()

    # MCP 鉴权在进程启动时绑定到中间件和 OAuth 路由可见性。有效值与期望持久值
    # 必须分开，避免 Dashboard 错称启动期切换已经热生效。
    runtime_mcp_auth_required = _parse_bool(
        sh.config.get("mcp_require_auth", True), default=True
    )
    runtime_mcp_auth_mode = _mcp_auth_mode(sh.config)
    runtime_transport = str(sh.config.get("transport") or "stdio")
    # deployment.public_url 参与 OAuth resource/audience 绑定，同样是启动快照。
    # Dashboard 往返使用独立期望值；重启前发布到 sh.config 会让已绑定的 OAuth
    # 路由与 MCP 中间件看到不同配置。
    runtime_public_url = configured_public_origin(sh.config)

    def _desired_startup_state(persisted: Mapping[str, object]) -> dict[str, object]:
        persisted_deployment = persisted.get("deployment")
        has_persisted_deployment = isinstance(persisted_deployment, Mapping)
        return {
            "transport": str(persisted.get("transport") or runtime_transport)
            if "transport" in persisted
            else runtime_transport,
            "mcp_require_auth": _parse_bool(
                persisted.get("mcp_require_auth"), default=runtime_mcp_auth_required
            )
            if "mcp_require_auth" in persisted
            else runtime_mcp_auth_required,
            "mcp_auth_mode": _mcp_auth_mode(persisted)
            if "mcp_auth_mode" in persisted
            else runtime_mcp_auth_mode,
            "public_url": configured_public_origin(persisted)
            if has_persisted_deployment
            else runtime_public_url,
        }

    def _runtime_network_security(desired_auth_required: object | None = None) -> dict:
        return current_mcp_network_security(
            sh.config,
            desired_auth_required=desired_auth_required,
            environment=os.environ,
            in_docker=sh.in_docker(),
        )

    @mcp.custom_route("/dashboard", methods=["GET"])
    async def dashboard(request: Request) -> Response:
        """Legacy alias: /dashboard 永久跳到根路径。

        我历史上把 dashboard 同时挂在 / 与 /dashboard，但叠加 Cloudflare 边缘
        （或任何 reverse proxy）的 host-rewrite 规则时容易触发回环。统一只在 /
        上提供 HTML，老书签靠 301 软迁移到 /。
        """
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=301)


    @mcp.custom_route("/api/env-vars", methods=["GET"])
    async def api_env_vars(request: Request) -> Response:
        """Return status of all known OMBRE_* env vars (sensitive fields masked)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        # 启动期被平台注入的可配置 env 集合（在任何 dashboard 保存 mutate os.environ 之前快照）。
        # from_boot=True ⇒ 该变量是平台级 env，重启后会覆盖 dashboard 存进 config.yaml 的值。
        from utils import BOOT_ENV_CONFIG

        def _masked(name: str) -> dict:
            return {"set": bool(os.environ.get(name, "").strip()), "value": None,
                    "from_boot": name in BOOT_ENV_CONFIG}

        def _plain(name: str) -> dict:
            v = os.environ.get(name, "").strip()
            return {"set": bool(v), "value": v or None, "from_boot": name in BOOT_ENV_CONFIG}

        vars_data = [
            # LLM 压缩组
            {"name": "OMBRE_COMPRESS_API_KEY", "group": "llm", "label": "压缩 LLM API Key", "sensitive": True, **_masked("OMBRE_COMPRESS_API_KEY")},
            {"name": "OMBRE_COMPRESS_BASE_URL", "group": "llm", "label": "压缩 LLM Base URL", "sensitive": False, **_plain("OMBRE_COMPRESS_BASE_URL")},
            {"name": "OMBRE_COMPRESS_MODEL", "group": "llm", "label": "压缩 LLM 模型", "sensitive": False, **_plain("OMBRE_COMPRESS_MODEL")},
            {"name": "OMBRE_COMPRESS_TIMEOUT_SECONDS", "group": "llm", "label": "压缩 LLM 超时秒数", "sensitive": False, **_plain("OMBRE_COMPRESS_TIMEOUT_SECONDS")},
            # Embedding 组
            {"name": "OMBRE_EMBED_API_KEY", "group": "embed", "label": "向量化 API Key", "sensitive": True, **_masked("OMBRE_EMBED_API_KEY")},
            {"name": "OMBRE_EMBED_BASE_URL", "group": "embed", "label": "向量化 Base URL", "sensitive": False, **_plain("OMBRE_EMBED_BASE_URL")},
            {"name": "OMBRE_EMBED_MODEL", "group": "embed", "label": "向量化模型", "sensitive": False, **_plain("OMBRE_EMBED_MODEL")},
            {"name": "OMBRE_EMBED_TIMEOUT_SECONDS", "group": "embed", "label": "向量化超时秒数", "sensitive": False, **_plain("OMBRE_EMBED_TIMEOUT_SECONDS")},
            # 服务配置组
            {"name": "OMBRE_TRANSPORT", "group": "system", "label": "传输模式", "sensitive": False, **_plain("OMBRE_TRANSPORT")},
            {"name": "OMBRE_PORT", "group": "system", "label": "服务端口", "sensitive": False, **_plain("OMBRE_PORT")},
            {"name": "OMBRE_LOG_FILE", "group": "system", "label": "日志文件路径", "sensitive": False, **_plain("OMBRE_LOG_FILE")},
            {"name": "OMBRE_CONFIG_PATH", "group": "system", "label": "配置文件路径", "sensitive": False, **_plain("OMBRE_CONFIG_PATH")},
            {"name": "OMBRE_MCP_REQUIRE_AUTH", "group": "auth", "label": "MCP 鉴权开关覆盖", "sensitive": False, **_plain("OMBRE_MCP_REQUIRE_AUTH")},
            {"name": "OMBRE_MCP_AUTH_MODE", "group": "auth", "label": "MCP 鉴权模式覆盖 (oauth/token/hybrid)", "sensitive": False, **_plain("OMBRE_MCP_AUTH_MODE")},
            {"name": "OMBRE_MCP_TOKEN", "group": "auth", "label": "MCP 静态 Token", "sensitive": True, **_masked("OMBRE_MCP_TOKEN")},
            {"name": "AI_NAME", "group": "identity", "label": "AI 显示名", "sensitive": False, **_plain("AI_NAME")},
            # 路径组
            {"name": "OMBRE_VAULT_DIR", "group": "paths", "label": "Vault 目录 (推荐)", "sensitive": False, **_plain("OMBRE_VAULT_DIR")},
            {"name": "OMBRE_BUCKETS_DIR", "group": "paths", "label": "桶目录 (旧版兼容)", "sensitive": False, **_plain("OMBRE_BUCKETS_DIR")},
            {"name": "OMBRE_HOST_VAULT_DIR", "group": "paths", "label": "宿主机 Vault 目录 (Docker)", "sensitive": False, **_plain("OMBRE_HOST_VAULT_DIR")},
            # Webhook 组
            {"name": "OMBRE_HOOK_URL", "group": "webhook", "label": "Webhook URL", "sensitive": False, **_plain("OMBRE_HOOK_URL")},
            {"name": "OMBRE_HOOK_SKIP", "group": "webhook", "label": "跳过 Webhook", "sensitive": False,
             "set": bool(os.environ.get("OMBRE_HOOK_SKIP", "").strip()),
             "value": os.environ.get("OMBRE_HOOK_SKIP", "").strip() or None},
            # 鉴权组
            {"name": "OMBRE_DASHBOARD_PASSWORD", "group": "auth", "label": "Dashboard 密码", "sensitive": True, **_masked("OMBRE_DASHBOARD_PASSWORD")},
        ]

        return JSONResponse({"vars": vars_data})


    async def _api_config_get_locked(request: Request) -> Response:
        """Get current runtime config (safe fields only, API key masked)."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            desired = _desired_startup_state(read_config_yaml())
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.error("persisted config read failed: err_type=%s", type(exc).__name__)
            return JSONResponse(
                {"error": "failed to read persisted config"},
                status_code=500,
            )
        dehy = sh.config.get("dehydration", {})
        emb = sh.config.get("embedding", {})
        runtime_network_security = _runtime_network_security(
            desired["mcp_require_auth"]
        )
        api_key = dehy.get("api_key", "")
        masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
        payload = {
            "dehydration": {
                "model": dehy.get("model", ""),
                "base_url": dehy.get("base_url", ""),
                "api_key_masked": masked_key,
                "max_tokens": dehy.get("max_tokens", 1024),
                "temperature": dehy.get("temperature", 0.1),
                "api_format": dehy.get("api_format", "openai_compat"),
                "timeout_seconds": dehy.get("timeout_seconds", 60),
            },
            "embedding": {
                "enabled": _parse_bool(emb.get("enabled", False), default=False),
                "model": emb.get("model", ""),
                "api_format": emb.get("api_format", "openai_compat"),
                "timeout_seconds": emb.get("timeout_seconds", 30),
                "backend": "api",
                "backend_options": [
                    {"value": "api", "label": "Gemini API（云端）", "note": "需填 OMBRE_EMBED_API_KEY，3072 维质量最高，需联网；客户端几乎不占额外内存"},
                ],
            },
            "surfacing": {
                "breath_max_results": int(sh.config.get("surfacing", {}).get("breath_max_results") or 20),
                "breath_max_tokens": int(sh.config.get("surfacing", {}).get("breath_max_tokens") or 10000),
                "feel_max_tokens": int(sh.config.get("surfacing", {}).get("feel_max_tokens") or 15000),
            },
            "merge_threshold": sh.config.get("merge_threshold", 75),
            # 只给日期不写时区时按它理解（Letter 定时锁等）。前端「设置」可改。
            "timezone": _get_timezone_name(),
            "transport": desired["transport"],
            "transport_effective": runtime_transport,
            "buckets_dir": sh.config.get("buckets_dir", ""),
            # MCP 鉴权开关。默认 true；具体 OAuth/静态 Token 模式由 mcp_auth_mode 决定。
            # 渲染一键开关；关掉后 /mcp 免认证直连（供自有前端 / GPT / GLM 等）。
            "mcp_require_auth": desired["mcp_require_auth"],
            "mcp_require_auth_effective": runtime_mcp_auth_required,
            # 鉴权模式（仅 mcp_require_auth=true 时有意义）：OAuth、静态 Token 或两者共存。
            "mcp_auth_mode": desired["mcp_auth_mode"],
            "mcp_auth_mode_effective": runtime_mcp_auth_mode,
            "mcp_network_security": runtime_network_security,
            # 静态 Token 状态：只回掩码/是否已配置，绝不回明文。
            "mcp_token_configured": bool(_current_mcp_token()),
            "mcp_token_hint": _mask_mcp_token(_current_mcp_token()),
            # Dashboard 的公网 MCP 地址是 OAuth resource/audience 的启动期
            # 配置；同时回传已保存值与本进程实际值，避免假装热切换成功。
            "deployment": {
                "public_url": desired["public_url"],
                "public_url_effective": runtime_public_url,
            },
            "restart_required": (
                (
                    desired["mcp_require_auth"] != runtime_mcp_auth_required
                    and not runtime_network_security.get("guard_active")
                    and not runtime_network_security.get("auth_environment_override")
                )
                or desired["mcp_auth_mode"] != runtime_mcp_auth_mode
                or desired["transport"] != runtime_transport
                or desired["public_url"] != runtime_public_url
            ),
            # 部署信息：数据目录 + 端口 + 是否容器内。前端「系统」区展示，端口可改。
            "host_port": sh.config.get("host_port"),
            "in_docker": sh.in_docker(),
            # AI 一方的显示名（取自环境变量 AI_NAME，回退 "AI"）。前端只读，用于
            # 面向用户的文案（如删除确认、信件署名占位）。
            "ai_name": _get_ai_name(),
            # 记忆归属：多人共用一套 OB 时标明「这份记忆是谁的」。owner_count>=2 时
            # 前端顶部才显示归属徽标（单人不打扰）；owner_name 为徽标文字。均只读。
            "owner_name": _get_owner_name(),
            "owner_count": _get_owner_count(),
        }
        payload.update(_extended_config_get_payload(sh.config))
        return JSONResponse(payload)


    @mcp.custom_route("/api/config", methods=["GET"])
    async def api_config_get(request: Request) -> Response:
        async with config_commit_lock:
            return await _api_config_get_locked(request)


    async def _api_config_update_locked(request: Request) -> Response:
        """Hot-update runtime sh.config. Optionally persist to config.yaml."""
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)

        updated = []
        try:
            persist_requested = _parse_bool(body.get("persist", False))
            persist_env_requested = _parse_bool(body.get("persist_env", False))
            (
                extended_sections,
                extended_env_updates,
                extended_gateway_hot,
                extended_restart_sections,
            ) = _sanitize_extended_config_payload(
                body, sh.config, persist_env_requested
            )
            mcp_auth_value = (
                _parse_bool(body["mcp_require_auth"])
                if "mcp_require_auth" in body
                else None
            )
            mcp_auth_mode_value = None
            if "mcp_auth_mode" in body:
                mcp_auth_mode_value = str(body["mcp_auth_mode"]).strip().lower()
                if mcp_auth_mode_value not in ("oauth", "token", "hybrid"):
                    return JSONResponse(
                        {"error": "mcp_auth_mode must be 'oauth', 'token', or 'hybrid'"},
                        status_code=400,
                    )
            embedding_payload = body.get("embedding")
            if "embedding" in body and not isinstance(embedding_payload, dict):
                return JSONResponse(
                    {"error": "embedding must be an object"}, status_code=400
                )
            if "dehydration" in body and not isinstance(
                body.get("dehydration"), dict
            ):
                return JSONResponse(
                    {"error": "dehydration must be an object"}, status_code=400
                )
            if "surfacing" in body and not isinstance(body.get("surfacing"), dict):
                return JSONResponse(
                    {"error": "surfacing must be an object"}, status_code=400
                )
            _dashboard_collect_model_api_key_updates(
                body, extended_env_updates, persist_env_requested
            )
            if not persist_requested and _dashboard_inline_secret_clear_required(body, sh.config):
                return JSONResponse(
                    {"error": "clearing a stored inline API key requires persist=true"}, status_code=400
                )
            dehydration_payload = dict(body.get("dehydration") or {})
            if "extra_body" in dehydration_payload and not isinstance(
                dehydration_payload["extra_body"], dict
            ):
                return JSONResponse(
                    {"error": "dehydration.extra_body must be an object"},
                    status_code=400,
                )
            if "max_tokens" in dehydration_payload:
                dehydration_payload["max_tokens"] = _bounded_config_int(
                    dehydration_payload["max_tokens"],
                    "dehydration.max_tokens",
                    128,
                    8192,
                )
            if "temperature" in dehydration_payload:
                dehydration_payload["temperature"] = _bounded_config_float(
                    dehydration_payload["temperature"],
                    "dehydration.temperature",
                    0.0,
                    2.0,
                )
            if "timeout_seconds" in dehydration_payload:
                dehydration_payload["timeout_seconds"] = _bounded_config_float(
                    dehydration_payload["timeout_seconds"],
                    "dehydration.timeout_seconds",
                    1.0,
                    600.0,
                )

            merge_threshold_value = (
                _bounded_config_int(
                    body["merge_threshold"], "merge_threshold", 0, 100
                )
                if "merge_threshold" in body
                else None
            )
            host_port_value = (
                _bounded_config_int(body["host_port"], "host_port", 1, 65535)
                if "host_port" in body
                else None
            )

            # --- Timezone ---
            # 只给日期不写时区时按它理解。必须当场校验：写进去一个解析不了的
            # 名字，之后每次解析日期都会静默回退 +08:00，用户以为自己设成功了。
            timezone_value = None
            if "timezone" in body:
                raw_tz = str(body.get("timezone") or "").strip()
                if raw_tz:
                    if len(raw_tz) > 64:
                        # 64 是随便定的。世界上最长的 IANA 时区名才 30 出头
                        # （America/Argentina/ComodRivadavia，去查了，真的存在）。
                        # 留一倍余量，剩下的当有人手滑。
                        return JSONResponse(
                            {"error": "timezone 名称过长"}, status_code=400
                        )
                    try:
                        from zoneinfo import ZoneInfo

                        ZoneInfo(raw_tz)
                    except Exception:
                        return JSONResponse(
                            {
                                "error": (
                                    f"无法识别时区「{raw_tz}」。请使用 IANA 时区名，"
                                    "例如 Asia/Shanghai、UTC、America/New_York。"
                                )
                            },
                            status_code=400,
                        )
                timezone_value = raw_tz

            surfacing_values: dict[str, int] = {}
            surfacing_payload = body.get("surfacing") or {}
            for key, low, high in (
                ("breath_max_results", 1, 50),
                ("breath_max_tokens", 500, 40000),
                ("feel_max_tokens", 500, 20000),
            ):
                if key in surfacing_payload:
                    surfacing_values[key] = _bounded_config_int(
                        surfacing_payload[key], f"surfacing.{key}", low, high
                    )
            deployment_payload = body.get("deployment")
            if "deployment" in body and not isinstance(deployment_payload, dict):
                return JSONResponse(
                    {"error": "deployment must be an object"}, status_code=400
                )
            deployment_public_url = None
            if isinstance(deployment_payload, dict) and "public_url" in deployment_payload:
                raw_public_url = str(deployment_payload["public_url"] or "").strip()
                deployment_public_url = ""
                if raw_public_url:
                    deployment_public_url = normalize_public_https_origin(
                        raw_public_url
                    )
                    if not deployment_public_url:
                        return JSONResponse(
                            {
                                "error": (
                                    "deployment.public_url must be an HTTPS domain "
                                    "or complete /mcp URL"
                                )
                            },
                            status_code=400,
                        )
            embedding_enabled = (
                _parse_bool(embedding_payload["enabled"])
                if isinstance(embedding_payload, dict)
                and "enabled" in embedding_payload
                else None
            )
            embedding_backend = None
            if isinstance(embedding_payload, dict) and "backend" in embedding_payload:
                backend_raw = str(embedding_payload["backend"]).strip().lower()
                embedding_backend = (
                    "api" if backend_raw in ("api", "gemini") else backend_raw
                )
                if embedding_backend != "api":
                    return JSONResponse(
                        {"error": f"unsupported embedding backend: {backend_raw}"},
                        status_code=400,
                    )
            sampling_payload = None
            if isinstance(body.get("surfacing"), dict):
                candidate = body["surfacing"].get("sampling")
                if candidate is not None and not isinstance(candidate, dict):
                    return JSONResponse(
                        {"error": "surfacing.sampling must be an object"},
                        status_code=400,
                    )
                sampling_payload = candidate
            sampling_enabled = (
                _parse_bool(sampling_payload["enabled"])
                if isinstance(sampling_payload, dict)
                and "enabled" in sampling_payload
                else None
            )
            sampling_values: dict[str, int | float] = {}
            if isinstance(sampling_payload, dict):
                if "top_k" in sampling_payload:
                    sampling_values["top_k"] = _bounded_config_int(
                        sampling_payload["top_k"],
                        "surfacing.sampling.top_k",
                        1,
                        50,
                    )
                if "sample_k" in sampling_payload:
                    sampling_values["sample_k"] = _bounded_config_int(
                        sampling_payload["sample_k"],
                        "surfacing.sampling.sample_k",
                        1,
                        20,
                    )
                if "temperature" in sampling_payload:
                    sampling_values["temperature"] = _bounded_config_float(
                        sampling_payload["temperature"],
                        "surfacing.sampling.temperature",
                        0.1,
                        5.0,
                    )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        startup_setting_requested = (
            deployment_public_url is not None
            or mcp_auth_value is not None
            or mcp_auth_mode_value is not None
        )
        if startup_setting_requested and not persist_requested:
            return JSONResponse(
                {
                    "error": (
                        "MCP startup settings require persist=true because "
                        "they only take effect after restart"
                    )
                },
                status_code=400,
            )
        hot_update_keys = {
            "dehydration",
            "embedding",
            "merge_threshold",
            "host_port",
            "surfacing",
            "timezone",
            *_EXTENDED_CONFIG_SECTIONS,
        }
        if startup_setting_requested and hot_update_keys.intersection(body):
            return JSONResponse(
                {
                    "error": (
                        "MCP startup settings cannot be combined with hot runtime "
                        "settings; save them in separate requests"
                    )
                },
                status_code=400,
            )

        mcp_network_security: dict | None = None
        if mcp_auth_value is False:
            # 先于任何热配置变更执行，避免同一请求稍后因危险鉴权设置被拒绝时，
            # 其他字段却已经部分生效；原子写入锁内还会基于最新磁盘配置再检查一次。
            security_candidate = dict(sh.config)
            security_candidate["mcp_require_auth"] = False
            mcp_network_security = assess_mcp_network_safety(
                security_candidate,
                environment=os.environ,
                in_docker=sh.in_docker(),
            )
            security_issue = mcp_network_safety_issue(mcp_network_security)
            if security_issue:
                return JSONResponse({
                    "error": security_issue,
                    "mcp_network_security": mcp_network_security,
                }, status_code=400)

        runtime_config_before = copy.deepcopy(sh.config)
        env_before = {name: os.environ.get(name) for name in extended_env_updates}
        try:
            env_snapshot = (
                _dashboard_env_snapshot() if extended_env_updates else None
            )
        except OSError as exc:
            logger.warning("extended env snapshot failed: err_type=%s", type(exc).__name__)
            return JSONResponse(
                {"error": "environment snapshot failed"}, status_code=500
            )
        # Publish the staged env view before rebuilding an engine.  EmbeddingEngine
        # follows the same env-over-config priority as a fresh process, so a clear
        # must remove the old process value before its replacement is constructed.
        for name, value in extended_env_updates.items():
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        embedding_before = sh.embedding_engine
        dehydrator_fields = (
            "model",
            "base_url",
            "max_tokens",
            "temperature",
            "timeout_seconds",
            "api_format",
            "extra_body",
            "api_key",
            "api_available",
            "client",
        )
        dehydrator_before = {
            field: getattr(sh.dehydrator, field)
            for field in dehydrator_fields
            if sh.dehydrator is not None and hasattr(sh.dehydrator, field)
        }

        def _rollback_hot_runtime() -> None:
            """在验证或持久化失败时恢复同一份运行态快照。"""
            sh.config.clear()
            sh.config.update(copy.deepcopy(runtime_config_before))
            if sh.dehydrator is not None:
                for field, value in dehydrator_before.items():
                    setattr(sh.dehydrator, field, value)
            if sh.embedding_engine is not embedding_before:
                sh.replace_embedding_engine(embedding_before)
            if env_snapshot is not None:
                _dashboard_restore_env(env_snapshot)
            for name, value in env_before.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        # --- Dehydration config ---
        if "dehydration" in body:
            d = dehydration_payload
            dehy = sh.config.setdefault("dehydration", {})
            for key in ("model", "base_url", "max_tokens", "temperature", "api_format", "timeout_seconds", "extra_body"):
                if key in d:
                    dehy[key] = d[key]
                    updated.append(f"dehydration.{key}")
            if _MODEL_API_KEY_ENV["dehydration"] in extended_env_updates:
                configured_key = extended_env_updates.get(
                    _MODEL_API_KEY_ENV["dehydration"], ""
                )
                if configured_key:
                    dehy["api_key"] = configured_key
                else:
                    dehy.pop("api_key", None)
                updated.append("dehydration.api_key")
            # 热重载压缩器：同步所有属性，让 Dashboard 修改立即生效。
            if sh.dehydrator is None:
                _rollback_hot_runtime()
                return JSONResponse(
                    {"error": "dehydration runtime unavailable"}, status_code=400
                )
            sh.dehydrator.model = dehy.get("model", sh.dehydrator.model)
            sh.dehydrator.base_url = dehy.get("base_url", sh.dehydrator.base_url)
            sh.dehydrator.max_tokens = int(dehy.get("max_tokens") or sh.dehydrator.max_tokens)
            configured_temperature = dehy.get("temperature")
            if configured_temperature is not None:
                sh.dehydrator.temperature = float(configured_temperature)
            sh.dehydrator.timeout_seconds = _positive_float(dehy.get("timeout_seconds"), sh.dehydrator.timeout_seconds)
            sh.dehydrator.api_format = dehy.get("api_format", getattr(sh.dehydrator, "api_format", "openai_compat"))
            sh.dehydrator.extra_body = dict(dehy.get("extra_body") or {})
            if _MODEL_API_KEY_ENV["dehydration"] in extended_env_updates:
                sh.dehydrator.api_key = dehy.get("api_key", "")
            sh.dehydrator.api_available = bool(sh.dehydrator.api_key)
            # 密钥或 URL 改变时重建 OpenAI 兼容客户端。
            if sh.dehydrator.api_available and sh.dehydrator.api_format == "openai_compat":
                from openai import AsyncOpenAI
                try:
                    sh.dehydrator.client = AsyncOpenAI(
                        api_key=sh.dehydrator.api_key,
                        base_url=sh.dehydrator.base_url,
                        timeout=sh.dehydrator.timeout_seconds,
                    )
                except Exception as exc:
                    _rollback_hot_runtime()
                    logger.warning(
                        "dehydration reload failed: err_type=%s detail=hidden",
                        type(exc).__name__,
                    )
                    return JSONResponse(
                        {"error": "dehydration reload failed"},
                        status_code=400,
                    )
            else:
                sh.dehydrator.client = None

        # --- Embedding config ---
        if "embedding" in body:
            e = embedding_payload
            emb = sh.config.setdefault("embedding", {})
            rebuild_embedding = False
            if embedding_enabled is not None:
                emb["enabled"] = embedding_enabled
                updated.append("embedding.enabled")
                rebuild_embedding = True
            if "model" in e:
                emb["model"] = e["model"]
                updated.append("embedding.model")
                rebuild_embedding = True
            if "base_url" in e:
                emb["base_url"] = str(e["base_url"]).strip()
                updated.append("embedding.base_url")
                rebuild_embedding = True
            if "timeout_seconds" in e:
                emb["timeout_seconds"] = e["timeout_seconds"]
                updated.append("embedding.timeout_seconds")
                rebuild_embedding = True
            if "api_format" in e:
                emb["api_format"] = str(e["api_format"]).strip()
                updated.append("embedding.api_format")
                rebuild_embedding = True
            if _MODEL_API_KEY_ENV["embedding"] in extended_env_updates:
                configured_key = extended_env_updates.get(
                    _MODEL_API_KEY_ENV["embedding"], ""
                )
                if configured_key:
                    emb["api_key"] = configured_key
                else:
                    emb.pop("api_key", None)
                updated.append("embedding.api_key")
                rebuild_embedding = True
            if embedding_backend is not None:
                emb["backend"] = embedding_backend
                updated.append("embedding.backend")
                rebuild_embedding = True

            # 一个请求可能修改多个字段；只重建一次，再把同一实例发布给 Web 路由、
            # BucketManager、ImportEngine 和 MCP 工具运行时，避免读写使用不同模型。
            if rebuild_embedding:
                try:
                    _rebuild_embedding_runtime()
                except Exception as e:
                    _rollback_hot_runtime()
                    logger.warning(
                        "embedding reload failed: err_type=%s detail=hidden",
                        type(e).__name__,
                    )
                    return JSONResponse(
                        {"error": "embedding reload failed"},
                        status_code=400,
                    )

        # --- Merge threshold ---
        if merge_threshold_value is not None:
            sh.config["merge_threshold"] = merge_threshold_value
            updated.append("merge_threshold")

        # --- Timezone ---
        if timezone_value is not None:
            sh.config["timezone"] = timezone_value
            updated.append("timezone")

        # MCP 鉴权开关、鉴权模式与公网地址都是启动期快照。它们只写入
        # config.yaml，不能提前发布到 sh.config；否则 OAuth/MCP 中间件仍使用
        # 旧闭包，而诊断与其他路由却会误以为新值已经生效。GET /api/config 会从
        # 持久配置回显 desired 值，并单独返回 effective 值。

        # --- 对外端口（host_port）---
        # 裸机：写 config 后进程自重启即监听新端口（前端「保存并重启」）。
        # Docker：容器内端口由 Dockerfile 固定，host_port 仅供部署脚本读取注入
        # OMBRE_HOST_PORT，须重建容器才生效（前端会提示）。
        if host_port_value is not None:
            sh.config["host_port"] = host_port_value
            updated.append("host_port")

        # --- Surfacing defaults (breath/feel token & result caps) ---
        if "surfacing" in body and isinstance(body["surfacing"], dict):
            sf = sh.config.setdefault("surfacing", {})
            for key, value in surfacing_values.items():
                sf[key] = value
                updated.append(f"surfacing.{key}")

        # --- Extended Dashboard contract ---
        # Store safe config fields in runtime; plaintext keys are runtime-only and
        # are supplied through environment variables to the Gateway/engines.
        for section_name, section_values in extended_sections.items():
            runtime_section = sh.config.setdefault(section_name, {})
            if not isinstance(runtime_section, dict):
                runtime_section = {}
                sh.config[section_name] = runtime_section
            _dashboard_merge_config_patch(runtime_section, section_values)
            for key, env_name in _SECRET_ENV_BY_SECTION.items():
                if section_name == key and env_name in extended_env_updates:
                    runtime_section["api_key"] = extended_env_updates[env_name]
            if section_name == "gateway" and _GATEWAY_SECRET_ENV in extended_env_updates:
                runtime_section["domain_sentinel_api_key"] = extended_env_updates[_GATEWAY_SECRET_ENV]
            updated.extend(f"{section_name}.{key}" for key in section_values)
            env_name = _SECRET_ENV_BY_SECTION.get(section_name)
            if env_name and env_name in extended_env_updates:
                updated.append(f"{section_name}.api_key")
            if section_name == "gateway":
                if _GATEWAY_SECRET_ENV in extended_env_updates:
                    updated.append("gateway.domain_sentinel_api_key")
                raw_gateway = body.get("gateway")
                raw_upstreams = raw_gateway.get("upstreams", []) if isinstance(raw_gateway, dict) else []
                upstream_env_names = {
                    env_name
                    for raw_upstream in raw_upstreams
                    if isinstance(raw_upstream, dict)
                    for env_name in _dashboard_sanitize_env_names(
                        raw_upstream.get("api_key_envs", raw_upstream.get("api_key_env", []))
                    )
                }
                if upstream_env_names.intersection(extended_env_updates):
                    updated.append("gateway.upstreams.api_keys")

        if extended_env_updates:
            try:
                updated.extend(_dashboard_write_env_updates(extended_env_updates))
            except Exception as exc:
                _rollback_hot_runtime()
                logger.warning("extended env update failed: err_type=%s", type(exc).__name__)
                return JSONResponse({"error": "environment persistence failed", "updated": []}, status_code=500)

        persisted_after: dict | None = None

        # --- Persist to config.yaml if requested ---
        if persist_requested:
            def _mutate(save_config: dict) -> None:
                if "dehydration" in body:
                    sc_dehy = save_config.setdefault("dehydration", {})
                    if not isinstance(sc_dehy, dict):
                        sc_dehy = {}
                        save_config["dehydration"] = sc_dehy
                    for key in ("model", "base_url", "max_tokens", "temperature", "api_format", "timeout_seconds", "extra_body"):
                        if key in dehydration_payload:
                            sc_dehy[key] = dehydration_payload[key]
                    # Never persist a submitted api_key to YAML; the paired env
                    # update above is the restart-visible source of truth.
                    if _MODEL_API_KEY_ENV["dehydration"] in extended_env_updates:
                        sc_dehy.pop("api_key", None)

                if "embedding" in body:
                    sc_emb = save_config.setdefault("embedding", {})
                    if not isinstance(sc_emb, dict):
                        sc_emb = {}
                        save_config["embedding"] = sc_emb
                    for key in ("model", "base_url", "api_format", "timeout_seconds"):
                        if key in body["embedding"]:
                            sc_emb[key] = body["embedding"][key]
                    if embedding_enabled is not None:
                        sc_emb["enabled"] = embedding_enabled
                    if embedding_backend is not None:
                        sc_emb["backend"] = embedding_backend
                    if _MODEL_API_KEY_ENV["embedding"] in extended_env_updates:
                        sc_emb.pop("api_key", None)

                if merge_threshold_value is not None:
                    save_config["merge_threshold"] = merge_threshold_value

                if timezone_value is not None:
                    save_config["timezone"] = timezone_value

                if mcp_auth_value is not None:
                    security_candidate = dict(save_config)
                    security_candidate.setdefault("transport", runtime_transport)
                    security_candidate["mcp_require_auth"] = mcp_auth_value
                    latest_security = assess_mcp_network_safety(
                        security_candidate,
                        environment=os.environ,
                        in_docker=sh.in_docker(),
                    )
                    security_issue = mcp_network_safety_issue(latest_security)
                    if security_issue:
                        raise ValueError(security_issue)
                    save_config["mcp_require_auth"] = mcp_auth_value

                if mcp_auth_mode_value is not None:
                    save_config["mcp_auth_mode"] = mcp_auth_mode_value

                if host_port_value is not None:
                    save_config["host_port"] = host_port_value

                if "surfacing" in body and isinstance(body["surfacing"], dict):
                    sc_sf = save_config.setdefault("surfacing", {})
                    if not isinstance(sc_sf, dict):
                        sc_sf = {}
                        save_config["surfacing"] = sc_sf
                    for key, value in surfacing_values.items():
                        sc_sf[key] = value
                    if "sampling" in body["surfacing"] and isinstance(body["surfacing"]["sampling"], dict):
                        sc_samp = sc_sf.setdefault("sampling", {})
                        if not isinstance(sc_samp, dict):
                            sc_samp = {}
                            sc_sf["sampling"] = sc_samp
                        if sampling_enabled is not None:
                            sc_samp["enabled"] = sampling_enabled
                        for key, value in sampling_values.items():
                            sc_samp[key] = value

                # Persist sanitized extended sections. New plaintext keys are written
                # only to .env, but preserve legacy inline keys when this save did not
                # replace them; otherwise changing an unrelated setting would silently
                # break an existing deployment on its next restart.
                for section_name, section_values in extended_sections.items():
                    saved_section = save_config.setdefault(section_name, {})
                    if not isinstance(saved_section, dict):
                        saved_section = {}
                        save_config[section_name] = saved_section
                    previous_section = dict(saved_section)
                    _dashboard_merge_config_patch(saved_section, section_values)
                    section_env_name = _SECRET_ENV_BY_SECTION.get(section_name)
                    section_secret_replaced = bool(section_env_name and section_env_name in extended_env_updates)
                    if section_secret_replaced:
                        saved_section.pop("api_key", None)
                        saved_section.pop("api_keys", None)
                    else:
                        for secret_key in ("api_key", "api_keys"):
                            if secret_key in previous_section:
                                saved_section[secret_key] = copy.deepcopy(previous_section[secret_key])
                    saved_section.pop("api_key_values", None)
                    if section_name == "gateway":
                        if _GATEWAY_SECRET_ENV in extended_env_updates:
                            saved_section.pop("domain_sentinel_api_key", None)
                        elif "domain_sentinel_api_key" in previous_section:
                            saved_section["domain_sentinel_api_key"] = copy.deepcopy(previous_section["domain_sentinel_api_key"])
                        previous_upstreams = previous_section.get("upstreams")
                        previous_by_name = {
                            str(item.get("name") or "").strip(): item
                            for item in previous_upstreams
                            if isinstance(item, dict) and str(item.get("name") or "").strip()
                        } if isinstance(previous_upstreams, list) else {}
                        raw_gateway = body.get("gateway")
                        raw_upstreams = (
                            raw_gateway.get("upstreams", [])
                            if isinstance(raw_gateway, dict)
                            else []
                        )
                        raw_upstreams_by_name = {
                            str(item.get("name") or "").strip(): item
                            for item in raw_upstreams
                            if isinstance(item, dict)
                            and str(item.get("name") or "").strip()
                        } if isinstance(raw_upstreams, list) else {}
                        if isinstance(saved_section.get("upstreams"), list):
                            for upstream_index, saved_upstream in enumerate(saved_section["upstreams"]):
                                if not isinstance(saved_upstream, dict):
                                    continue
                                previous_upstream = previous_by_name.get(str(saved_upstream.get("name") or "").strip())
                                if previous_upstream is None and isinstance(previous_upstreams, list) and upstream_index < len(previous_upstreams) and isinstance(previous_upstreams[upstream_index], dict):
                                    candidate = previous_upstreams[upstream_index]
                                    if not candidate.get("name") and saved_upstream.get("name") == f"upstream-{upstream_index + 1}":
                                        previous_upstream = candidate
                                previous_upstream = previous_upstream or {}
                                env_names = _dashboard_sanitize_env_names(saved_upstream.get("api_key_envs", saved_upstream.get("api_key_env", [])))
                                raw_upstream = raw_upstreams_by_name.get(
                                    str(saved_upstream.get("name") or "").strip()
                                )
                                if raw_upstream is None and isinstance(raw_upstreams, list) and upstream_index < len(raw_upstreams) and isinstance(raw_upstreams[upstream_index], dict):
                                    raw_upstream = raw_upstreams[upstream_index]
                                upstream_secret_replaced = bool(
                                    set(env_names).intersection(extended_env_updates)
                                )
                                inline_secret_cleared = bool(
                                    raw_upstream
                                    and _dashboard_upstream_clear_inline_keys(raw_upstream)
                                )
                                if upstream_secret_replaced or inline_secret_cleared:
                                    saved_upstream.pop("api_key", None)
                                    saved_upstream.pop("api_keys", None)
                                else:
                                    for secret_key in ("api_key", "api_keys"):
                                        if secret_key in previous_upstream:
                                            saved_upstream[secret_key] = copy.deepcopy(previous_upstream[secret_key])
                                saved_upstream.pop("api_key_values", None)

                for section_name, env_name in _MODEL_API_KEY_ENV.items():
                    raw_section = body.get(section_name)
                    if not isinstance(raw_section, dict) or env_name not in extended_env_updates:
                        continue
                    saved_section = save_config.setdefault(section_name, {})
                    if not isinstance(saved_section, dict):
                        saved_section = {}
                        save_config[section_name] = saved_section
                    saved_section.pop("api_key", None)

                if deployment_public_url is not None:
                    sc_deployment = save_config.get("deployment")
                    if not isinstance(sc_deployment, dict):
                        sc_deployment = {}
                        save_config["deployment"] = sc_deployment
                    if deployment_public_url:
                        sc_deployment["public_url"] = deployment_public_url
                    else:
                        sc_deployment.pop("public_url", None)

            try:
                persisted_after = atomic_update_config_yaml(_mutate)
                updated.append("persisted_to_yaml")
                if mcp_auth_value is not None:
                    updated.append("mcp_require_auth")
                if mcp_auth_mode_value is not None:
                    updated.append("mcp_auth_mode")
                if deployment_public_url is not None:
                    updated.append("deployment.public_url")
            except ValueError as e:
                _rollback_hot_runtime()
                return JSONResponse({"error": str(e), "updated": []}, status_code=400)
            except Exception as e:
                _rollback_hot_runtime()
                logger.error(
                    "config persist failed: err_type=%s detail=hidden",
                    type(e).__name__,
                )
                return JSONResponse(
                    {"error": "persist failed", "updated": []},
                    status_code=500,
                )

        # Persist first, then ask the separately running Gateway to reload. A missing
        # admin channel is reported as a warning rather than a false success.
        warnings: list[str] = []
        if extended_gateway_hot:
            hot_status = await _hot_update_gateway_config(extended_gateway_hot)
            if hot_status == "gateway_hot_reloaded":
                updated.append(hot_status)
            elif hot_status:
                warnings.append(hot_status)
            else:
                warnings.append(
                    (
                        "Gateway 热更新未执行：未配置 OMBRE_GATEWAY_ADMIN_URL/OMBRE_GATEWAY_TOKEN；"
                        + (
                            "已持久化配置将在 Gateway 重启后生效。"
                            if persist_requested
                            else "本次只更新了 Dashboard 当前运行态，Gateway 未同步。"
                        )
                    )
                )
        if extended_restart_sections:
            warnings.append(
                "以下配置已保存，但当前进程没有安全的完整重建入口，需要重启后生效："
                + ", ".join(extended_restart_sections)
            )

        desired = _desired_startup_state(
            persisted_after if persisted_after is not None else sh.config
        )
        runtime_network_security = _runtime_network_security(
            desired["mcp_require_auth"]
        )
        auth_environment_conflict = (
            runtime_network_security.get("auth_environment_override")
            and runtime_network_security.get("auth_environment_value")
            != desired["mcp_require_auth"]
        )
        restart_required = bool(extended_restart_sections) or (
            (
                desired["mcp_require_auth"] != runtime_mcp_auth_required
                and not runtime_network_security.get("guard_active")
                and not runtime_network_security.get("auth_environment_override")
            )
            or desired["mcp_auth_mode"] != runtime_mcp_auth_mode
            or desired["transport"] != runtime_transport
            or desired["public_url"] != runtime_public_url
        )
        return JSONResponse({
            "updated": updated,
            "ok": True,
            "restart_required": restart_required,
            "mcp_require_auth_effective": runtime_mcp_auth_required,
            "mcp_auth_mode_effective": runtime_mcp_auth_mode,
            "transport": desired["transport"],
            "transport_effective": runtime_transport,
            "mcp_require_auth": desired["mcp_require_auth"],
            "mcp_auth_mode": desired["mcp_auth_mode"],
            "mcp_network_security": runtime_network_security,
            "warnings": (
                warnings
                + (
                    [runtime_network_security["reason"]]
                    if runtime_network_security.get("override_active") else []
                )
                + (
                    [
                        "OMBRE_MCP_REQUIRE_AUTH 仍由平台环境变量控制；"
                        "请在部署平台修改或删除该变量后重建/重启服务。"
                    ]
                    if auth_environment_conflict else []
                )
            ),
            "deployment": {
                "public_url": desired["public_url"],
                "public_url_effective": runtime_public_url,
            },
            "message": (
                "设置已保存；当前配置或环境仍请求免鉴权，安全门禁继续强制鉴权。"
                if runtime_network_security.get("guard_active")
                else (
                    "设置已保存，但 OMBRE_MCP_REQUIRE_AUTH 仍由平台环境变量控制；"
                    "请在部署平台修改或删除该变量后重建/重启服务。"
                    if auth_environment_conflict
                    else (
                        "MCP 启动配置已保存，需要重启服务后生效。"
                        if restart_required else "设置已生效。"
                    )
                )
            ),
        })


    @mcp.custom_route("/api/config", methods=["POST"])
    async def api_config_update(request: Request) -> Response:
        async with config_commit_lock:
            return await _api_config_update_locked(request)


    # =============================================================
    # /api/mcp-token/regenerate — 生成/轮换 token/hybrid 模式使用的静态密钥
    # 独立成一个小路由（而不是塞进 POST /api/config）：生成新密钥和改配置项
    # 是两件不同的事，参照 oauth.py 里 token 签发自成一块的做法。
    # =============================================================
    @mcp.custom_route("/api/mcp-token/regenerate", methods=["POST"])
    async def api_mcp_token_regenerate(request: Request) -> Response:
        """(Re)generate the static MCP token and persist it to config.yaml.

        Returns the plaintext token exactly once — GET /api/config only ever
        returns a masked hint, so the Dashboard must capture this response.
        Takes effect immediately when the running process is already in token
        or hybrid mode: _is_valid_static_mcp_token reads sh.config/env fresh on
        every request. A newly selected auth mode still requires a restart.
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        new_token = secrets.token_urlsafe(32)

        # 原生锁覆盖“落盘 + 发布”整个提交段，且临界区内没有 await；既避免
        # 并发请求发生磁盘 B、运行态 A 的逆序，也不产生 asyncio 跨循环绑定。
        with mcp_token_commit_lock:
            try:
                atomic_update_config_yaml(
                    lambda save_config: save_config.__setitem__(
                        "mcp_token", new_token
                    )
                )
            except Exception as e:
                return JSONResponse(
                    {"error": f"persist failed: {e}"}, status_code=500
                )

            # 鉴权每次请求都直接读取 sh.config；必须先确认持久化成功，再发布运行态。
            # 否则磁盘写失败时接口虽然返回 500，新 token 却已经即时生效，重启后又
            # 回到旧 token，形成无法从响应判断的临时授权状态。
            sh.config["mcp_token"] = new_token

        env_override = bool(os.environ.get("OMBRE_MCP_TOKEN", "").strip())
        return JSONResponse({
            "ok": True,
            "token": new_token,
            "token_hint": _mask_mcp_token(new_token),
            "env_override": env_override,
            "message": (
                "环境变量 OMBRE_MCP_TOKEN 优先级更高，已生成的新密钥暂不会生效，"
                "请改用该环境变量或先取消设置它。"
                if env_override
                else "新 Token 已生成并保存，请立即复制；刷新页面后不再显示完整值。"
                     "当前进程已处于 Token/共存模式时立即生效；刚切换模式仍需重启。"
            ),
        })


    # =============================================================
    # /api/test/dehydration — 测试脱水 LLM API Key 是否可用
    # =============================================================
    @mcp.custom_route("/api/test/dehydration", methods=["POST"])
    async def api_test_dehydration(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        # Use current runtime config (api_key may have been updated in-memory)
        dehyd = sh.config.get("dehydration", {})
        model = dehyd.get("model", "")
        base_url = dehyd.get("base_url", "")
        api_key = dehyd.get("api_key", "")
        if not api_key:
            return JSONResponse({"ok": False, "error": "未设置 API Key"}, status_code=400)
        try:
            import httpx as _httpx
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
                **chat_completion_token_limit(model, 5),
            }
            async with _httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers)
            if r.status_code in (200, 201):
                return JSONResponse({"ok": True, "message": "API Key 有效 ✓"})
            else:
                try:
                    detail = r.json().get("error", {})
                    msg = detail.get("message", r.text[:200]) if isinstance(detail, dict) else str(detail)[:200]
                except Exception:
                    msg = r.text[:200]
                return JSONResponse({"ok": False, "error": f"HTTP {r.status_code}: {msg}"})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]})


    # =============================================================
    # /api/test/embedding — 测试向量化 Embedding 是否真的可用
    # 之前只有脱水(compress)能测，向量化无从验证 → 用户「压缩正常但向量化静默失败」
    # 时完全无感。这里实际发一次 embedding 请求，把成功/失败如实回给前端。(#2/#3)
    # =============================================================
    @mcp.custom_route("/api/test/embedding", methods=["POST"])
    async def api_test_embedding(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        eng = sh.embedding_engine  # 读全局（Fix: env-sh.config 保存后已正确重建）
        if not getattr(eng, "enabled", False) or getattr(eng, "_backend", None) is None:
            return JSONResponse({
                "ok": False,
                "error": "向量化未启用或缺 key（standby）。请填入 Embedding API Key 点「保存」后再测。",
            })
        try:
            vec = await eng._generate_async("connectivity probe / 连接性探针")
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
        if vec:
            model = getattr(eng, "model", "") or (
                eng._backend.model_name() if getattr(eng, "_backend", None) else "?"
            )
            return JSONResponse({
                "ok": True,
                "message": f"向量化连接成功 ✓（模型 {model}，维度 {len(vec)}）",
            })
        return JSONResponse({
            "ok": False,
            "error": "调用返回空向量：检查 model 名 / base_url / key 是否匹配该 provider"
                     "（如硅基流动 base_url=https://api.siliconflow.cn/v1、model=BAAI/bge-m3）。详见错误面板 OB-E001。",
        })


    # =============================================================
    # /api/models — 获取 LLM provider 可用模型列表（供 Dashboard 模型选择器使用）
    # POST Body: {api_key, base_url, api_format}
    # 支持 openai_compat / gemini / anthropic 三种格式
    # =============================================================
    @mcp.custom_route("/api/models", methods=["POST"])
    async def api_list_models(request: Request) -> Response:
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

        provider_fields = ("api_key", "base_url", "api_format")
        if any(key in body and not isinstance(body[key], str) for key in provider_fields):
            return JSONResponse({"ok": False, "error": "provider fields must be strings"}, status_code=400)
        api_key = str(body.get("api_key", "")).strip()
        base_url = str(body.get("base_url", "")).strip()
        api_format = str(body.get("api_format", "openai_compat")).strip().lower()
        if (
            len(api_key) > _MAX_PROVIDER_KEY_CHARS
            or len(base_url) > _MAX_PROVIDER_URL_CHARS
            or len(api_format) > _MAX_PROVIDER_FORMAT_CHARS
        ):
            return JSONResponse({"ok": False, "error": "provider configuration is too large"}, status_code=400)

        # Sentinel "__use_current__": use server-side key from dehydration config
        if api_key == "__use_current__":
            api_key = sh.config.get("dehydration", {}).get("api_key", "")
            if not base_url:
                base_url = sh.config.get("dehydration", {}).get("base_url", "")
            if not api_format or api_format == "openai_compat":
                api_format = sh.config.get("dehydration", {}).get("api_format", "openai_compat")
        # Sentinel "__use_current_embed__": use server-side key from embedding config
        if api_key == "__use_current_embed__":
            api_key = sh.config.get("embedding", {}).get("api_key", "")
            if not base_url:
                base_url = sh.config.get("embedding", {}).get("base_url", "")

        if not api_key:
            return JSONResponse({"ok": False, "error": "需要 api_key（请先保存 API Key 或在输入框填入）"}, status_code=400)

        try:
            models: list[str] = []
            if api_format in ("gemini", "gemini_embed"):
                # gemini → generateContent models；gemini_embed → embedContent models
                method_filter = "embedContent" if api_format == "gemini_embed" else "generateContent"
                url = "https://generativelanguage.googleapis.com/v1beta/models"
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.get(
                        url,
                        params={"pageSize": 200},
                        headers={"x-goog-api-key": api_key},
                    )
                r.raise_for_status()
                for m in r.json().get("models", []):
                    if method_filter in m.get("supportedGenerationMethods", []):
                        models.append(m.get("name", "").replace("models/", ""))
            elif api_format == "anthropic":
                ant_base = base_url.rstrip("/") if base_url else "https://api.anthropic.com"
                headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.get(f"{ant_base}/v1/models", headers=headers)
                r.raise_for_status()
                models = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
            else:  # openai_compat
                if not base_url:
                    return JSONResponse({"ok": False, "error": "openai_compat 格式需要 base_url"}, status_code=400)
                headers_oai = {"Authorization": f"Bearer {api_key}"}
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.get(f"{base_url.rstrip('/')}/models", headers=headers_oai)
                r.raise_for_status()
                models = sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))
            return JSONResponse({"ok": True, "models": [m for m in models if m]})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]})


    # =============================================================
    # /api/env-config — Dashboard 热更新环境变量（四块：Compress / Embed / Password / Webhook）
    # GET  返回当前值（API key 脱敏）
    # POST 批量更新：同时更新进程内 config + 写 .env 文件持久化
    # =============================================================

    # 哪些变量可以从 Dashboard 读写（不能出现在这里之外的变量）
    _ENV_CONFIG_FIELDS: dict[str, dict] = {
        # Compress / 脱水压缩
        "OMBRE_COMPRESS_API_KEY":  {"group": "compress", "sensitive": True,  "in_memory": ("dehydration", "api_key")},
        "OMBRE_COMPRESS_BASE_URL": {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "base_url")},
        "OMBRE_COMPRESS_MODEL":    {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "model")},
        "OMBRE_COMPRESS_FORMAT":   {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "api_format")},
        "OMBRE_COMPRESS_TIMEOUT_SECONDS": {"group": "compress", "sensitive": False, "in_memory": ("dehydration", "timeout_seconds")},
        # Embed / 向量化（backend 切换走 /api/embedding/migrate）
        "OMBRE_EMBED_API_KEY":     {"group": "embed",    "sensitive": True,  "in_memory": ("embedding", "api_key")},
        "OMBRE_EMBED_BASE_URL":    {"group": "embed",    "sensitive": False, "in_memory": ("embedding", "base_url")},
        "OMBRE_EMBED_MODEL":       {"group": "embed",    "sensitive": False, "in_memory": ("embedding", "model")},
        "OMBRE_EMBED_FORMAT":      {"group": "embed",    "sensitive": False, "in_memory": ("embedding", "api_format")},
        "OMBRE_EMBED_TIMEOUT_SECONDS": {"group": "embed", "sensitive": False, "in_memory": ("embedding", "timeout_seconds")},
        # Webhook
        "OMBRE_HOOK_URL":          {"group": "webhook",  "sensitive": False, "in_memory": None},
        "OMBRE_HOOK_SKIP":         {"group": "webhook",  "sensitive": False, "in_memory": None},
        # Identity / display labels
        "AI_NAME":                 {"group": "identity", "sensitive": False, "in_memory": None},
    }

    _ENV_CONFIG_NOTE = {
        "compress": "改完即时生效（进程内 sh.config 已更新），同时写 config.yaml 持久化（重启后仍有效）。",
        "embed": "API key / base_url / model 立即更新进程内 config；backend 切换请用「切换 / 重算所有 embedding…」按钮。",
        "webhook": "改完下次 breath/dream 触发时即生效，无需重启。",
        "identity": "AI 显示名立即生效；若由平台环境变量注入，重启后仍会被平台值覆盖。",
    }


    def _mask(val: str) -> str:
        """对 API key 做脱敏，末 4 位保留供校验。"""
        if not val:
            return ""
        if len(val) > 8:
            return f"{val[:4]}...{val[-4:]}"
        return "***"


    @mcp.custom_route("/api/env-config", methods=["GET"])
    async def api_env_config_get(request: Request) -> Response:
        """
        返回四块配置的当前值（API key 脱敏显示）。
        优先读进程内 sh.config / os.environ，其次读 .env 文件。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        result: dict[str, dict] = {}
        for var, meta in _ENV_CONFIG_FIELDS.items():
            # 优先从 config dict 读（进程内最新）
            raw = ""
            if meta["in_memory"]:
                section, key = meta["in_memory"]
                raw = str(sh.config.get(section, {}).get(key, "")).strip()
            # 进程内为空，则读 os.environ
            if not raw:
                raw = os.environ.get(var, "").strip()
            # 再读 .env 文件
            if not raw:
                raw = sh._read_env_var(var)
            result[var] = {
                "group": meta["group"],
                "sensitive": meta["sensitive"],
                "value": _mask(raw) if meta["sensitive"] else raw,
                "is_set": bool(raw),
            }

        return JSONResponse({
            "ok": True,
            "fields": result,
            "notes": _ENV_CONFIG_NOTE,
        })


    @mcp.custom_route("/api/env-config", methods=["POST"])
    async def api_env_config_set(request: Request) -> Response:
        """
        热更新指定环境变量。

        Body (JSON): {"updates": {"OMBRE_COMPRESS_API_KEY": "sk-...", ...}}
        - 只写传入的字段，未传字段不动。
        - 空字符串 = 清除该变量（.env 里写成 NAME= ，进程内 sh.config 设为 ""）。
        - API key 不支持 "***" 保持不变（应传实际值或空字符串）。

        返回字段：
        - updated：已写入当前进程 sh.config / os.environ 的变量名；若对应
          业务引擎热更新失败，会同时出现在 warnings 中；
        - persisted：已成功落盘、重启后仍会保留的变量名；
        - partial / warnings：运行时已生效但落盘失败，或部分字段未应用。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err

        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

        updates: dict = body.get("updates", {})
        if not isinstance(updates, dict) or not updates:
            return JSONResponse({"ok": False, "error": "updates 必须是非空对象"}, status_code=400)
        if len(updates) > len(_ENV_CONFIG_FIELDS):
            return JSONResponse({"ok": False, "error": "updates 字段过多"}, status_code=400)

        accepted: dict[str, str] = {}
        warnings: list[str] = []

        for var, val in updates.items():
            if var not in _ENV_CONFIG_FIELDS:
                warnings.append(f"{var}: 不在白名单里，未应用")
                continue
            if not isinstance(val, str):
                warnings.append(f"{var}: 值必须是字符串，未应用")
                continue
            if len(val) > _MAX_ENV_VALUE_CHARS:
                warnings.append(f"{var}: 值超过 {_MAX_ENV_VALUE_CHARS} 字符，未应用")
                continue
            # 拒绝明显的注入字符
            if "\n" in val or "\r" in val:
                warnings.append(f"{var}: 值不能含换行，未应用")
                continue

            value = val.strip()

            # OMBRE_HOOK_URL 只允许 http/https（防止意外配成 file:// 等非 HTTP scheme）
            if var == "OMBRE_HOOK_URL" and value and not value.startswith(("http://", "https://")):
                warnings.append(f"{var}: 只允许 http:// 或 https:// 开头的 URL，未应用")
                continue

            accepted[var] = value

        # Compress 必须按整批最终配置只重建一次 client。逐字段重建会让请求中的
        # key/base_url/model 顺序影响中间状态，也会在落盘失败时留下旧 client。
        compress_vars = [
            var for var in accepted
            if _ENV_CONFIG_FIELDS[var]["group"] == "compress"
        ]
        if compress_vars:
            current_dehy = sh.dehydrator
            current_cfg = sh.config.get("dehydration", {})
            staged_cfg = dict(current_cfg) if isinstance(current_cfg, dict) else {}
            for var in compress_vars:
                _section, key = _ENV_CONFIG_FIELDS[var]["in_memory"]
                staged_cfg[key] = accepted[var]

            try:
                if current_dehy is None:
                    raise RuntimeError("dehydrator runtime unavailable")
                staged_api_key = staged_cfg.get(
                    "api_key", getattr(current_dehy, "api_key", "")
                )
                staged_base_url = staged_cfg.get(
                    "base_url", getattr(current_dehy, "base_url", "")
                )
                staged_model = staged_cfg.get(
                    "model", getattr(current_dehy, "model", "")
                )
                staged_timeout = _positive_float(
                    staged_cfg.get("timeout_seconds"),
                    getattr(current_dehy, "timeout_seconds", 60.0),
                )
                staged_format = staged_cfg.get(
                    "api_format", getattr(current_dehy, "api_format", "openai_compat")
                )
                staged_available = bool(staged_api_key)
                staged_client = None
                if staged_available and staged_format == "openai_compat":
                    from openai import AsyncOpenAI as _OAI_DH

                    staged_client = _OAI_DH(
                        api_key=staged_api_key,
                        base_url=staged_base_url,
                        timeout=staged_timeout,
                    )

                staged_attrs = {
                    "api_key": staged_api_key,
                    "base_url": staged_base_url,
                    "model": staged_model,
                    "timeout_seconds": staged_timeout,
                    "api_format": staged_format,
                    "api_available": staged_available,
                    "client": staged_client,
                }
                previous_attrs = {
                    name: getattr(current_dehy, name) for name in staged_attrs
                }
                try:
                    for name, value in staged_attrs.items():
                        setattr(current_dehy, name, value)
                except Exception:
                    for name, value in previous_attrs.items():
                        try:
                            setattr(current_dehy, name, value)
                        except Exception:
                            pass
                    raise
            except Exception as e:
                failed = ", ".join(compress_vars)
                warnings.append(
                    f"压缩配置热更新失败，未应用 {failed}：{type(e).__name__}: {e}"
                )
                for var in compress_vars:
                    accepted.pop(var, None)

        # 运行时更新与持久化解耦。到这里的字段先全部对当前进程生效；后续落盘
        # 即使失败，也不能阻断 dehydrator/client 已完成的热更新。
        written: list[str] = []
        for var, value in accepted.items():
            meta = _ENV_CONFIG_FIELDS[var]
            if meta["in_memory"]:
                section, key = meta["in_memory"]
                section_cfg = sh.config.get(section)
                if not isinstance(section_cfg, dict):
                    section_cfg = {}
                    sh.config[section] = section_cfg
                section_cfg[key] = value
            if value:
                os.environ[var] = value
            else:
                os.environ.pop(var, None)
            written.append(var)

        # Embed 配置同样等整批字段进入 sh.config 后再重建一次，避免先用旧 URL
        # 建引擎、下一字段才补 model/key。失败时明确报告，不再静默吞掉。
        embed_vars = [
            var for var in written
            if _ENV_CONFIG_FIELDS[var]["group"] == "embed"
        ]
        if embed_vars:
            try:
                if (
                    "OMBRE_EMBED_API_KEY" in embed_vars
                    and not accepted["OMBRE_EMBED_API_KEY"]
                ):
                    sh.embedding_engine._backend = None  # type: ignore[attr-defined]
                    sh.embedding_engine.enabled = False
                    sh.replace_embedding_engine(sh.embedding_engine)
                else:
                    _rebuild_embedding_runtime()
            except Exception as e:
                warnings.append(
                    "向量化配置已写入进程配置，但运行时引擎重建失败："
                    f"{type(e).__name__}: {e}"
                )

        persisted: list[str] = []

        # 没有 config.yaml 映射的字段写项目 .env；失败不撤销已生效的 os.environ。
        for var in written:
            if _ENV_CONFIG_FIELDS[var]["in_memory"]:
                continue
            try:
                sh._write_env_var(var, accepted[var])
                persisted.append(var)
            except Exception as e:
                warnings.append(
                    f"{var}: 运行时已生效，但写 .env 失败，重启后可能丢失：{e}"
                )

        # 所有映射到 config.yaml 的字段一次原子落盘，保证多字段配置不会只写一半。
        yaml_vars = [
            var for var in written if _ENV_CONFIG_FIELDS[var]["in_memory"]
        ]
        if yaml_vars:
            def _persist_batch(save_config: dict) -> None:
                for var in yaml_vars:
                    section, key = _ENV_CONFIG_FIELDS[var]["in_memory"]
                    section_cfg = save_config.get(section)
                    if not isinstance(section_cfg, dict):
                        section_cfg = {}
                        save_config[section] = section_cfg
                    section_cfg[key] = accepted[var]

            try:
                atomic_update_config_yaml(_persist_batch)
                persisted.extend(yaml_vars)
            except Exception as e:
                affected = ", ".join(yaml_vars)
                warnings.append(
                    "运行时已生效，但 config.yaml 持久化失败；重启后可能恢复旧值"
                    f"（{affected}）：{type(e).__name__}: {e}"
                )

        partial = bool(warnings) or len(written) != len(updates)
        response: dict = {
            "ok": bool(written),
            "partial": bool(written) and partial,
            "updated": written,
            "persisted": persisted,
            "env_file": sh._project_env_path(),
            "note": (
                "updated 中字段已写入进程配置；若 warnings 指出引擎重建失败，"
                "则对应业务引擎尚未生效。仅 persisted 中的字段确认已落盘。"
                if partial
                else "当前进程运行时与持久化配置均已更新。"
            ),
        }
        if warnings:
            response["warnings"] = warnings
        if not written:
            response["error"] = warnings[0] if warnings else "没有字段成功更新"
        return JSONResponse(response)


    # --- 传输模式热切换：streamable-http / stdio ---
    # transport 是「启动时绑定」的（server.py 据此起 streamable_http_app / stdio），
    # 运行中无法无缝切换，所以这里的做法是：持久化新值 → 原地自重启（os.execv 继承已改的
    # os.environ，绕过 compose 里硬编码的旧 OMBRE_TRANSPORT）→ 新进程按新 transport 起。
    # 2026-08-09 起 legacy SSE（"sse"）已下线，不再是可选项。
    _TRANSPORT_CHOICES = ("streamable-http", "stdio")

    @mcp.custom_route("/api/transport", methods=["POST"])
    async def api_transport_set(request: Request) -> Response:
        """切换 MCP 传输模式并自重启生效。

        Body (JSON): {"transport": "streamable-http" | "stdio"}

        ⚠️ stdio 没有 HTTP 服务：切到 stdio 后 Dashboard / REST / /mcp(HTTP) 全部消失，
        且无法再从网页切回（需在服务器改 config.yaml / env 恢复）。前端对此二次确认。
        """
        from starlette.responses import JSONResponse
        err = sh._require_auth(request)
        if err:
            return err
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

        new_t = str(body.get("transport") or "").strip()
        if new_t not in _TRANSPORT_CHOICES:
            return JSONResponse(
                {"ok": False, "error": f"transport 必须是 {list(_TRANSPORT_CHOICES)} 之一"},
                status_code=400,
            )

        current = str(sh.config.get("transport", "stdio"))
        if new_t == current:
            return JSONResponse({"ok": True, "transport": new_t, "restarting": False,
                                 "note": "传输模式未变化，无需重启。"})

        # 1. 运行时 config + os.environ（os.execv 自重启会继承 environ，
        #    从而盖过 docker-compose 里硬编码的旧 OMBRE_TRANSPORT）。
        sh.config["transport"] = new_t
        os.environ["OMBRE_TRANSPORT"] = new_t

        # 2. 持久化到项目 .env（compose 若以 ${OMBRE_TRANSPORT} 引用则容器重建也保留）。
        env_persisted = True
        try:
            sh._write_env_var("OMBRE_TRANSPORT", new_t)
        except Exception:
            env_persisted = False

        # 3. 持久化到 config.yaml（裸机 / 无 env 覆盖时的权威来源）。
        try:
            atomic_update_config_yaml(lambda saved: saved.__setitem__("transport", new_t))
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"写 config.yaml 失败：{e}"}, status_code=500)

        # 4. 延迟自重启，让本次响应先回到前端（参照 /api/do-update 的重启节奏）。
        import threading

        def _do_restart() -> None:
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                os._exit(0)

        threading.Timer(1.0, _do_restart).start()
        logger.info(f"[transport] 切换 {current} → {new_t}，1s 后自重启生效")
        return JSONResponse({
            "ok": True,
            "transport": new_t,
            "previous": current,
            "restarting": True,
            "env_persisted": env_persisted,
            "loses_http": new_t == "stdio",
        })
