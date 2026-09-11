"""Read-only compatibility projections for the former Haven dashboard APIs.

This module intentionally does *not* import the repository-root Haven modules.
The v3 server puts ``src`` before the repository root on ``sys.path`` and the
old flat modules (``utils``, ``errors``, ``bucket_manager`` and friends) have
colliding names.  Loading that graph from a request would be unsafe anyway:
several old constructors initialise databases, create directories, or start
clients.  The compatibility surface therefore reads existing files directly,
and uses only the already-injected v3 bucket manager for bucket projections.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _shared as sh

logger = sh.logger
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_REMINDER_FIELDS = (
    "id", "title", "content", "status", "source", "channel", "session_id",
    "start_at", "end_at", "next_due_at", "repeat_rule", "interval_rounds",
    "cooldown_minutes", "daily_limit", "daily_reminder_date", "daily_reminder_count",
    "max_injections", "last_reminded_at", "last_reminded_round", "reminder_count",
    "created_at", "updated_at", "resolved_at",
)
_PERSONA_STATE_FIELDS = (
    "profile_id", "affinity", "dominance", "defensiveness", "trust", "updated_at",
    "openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism",
)
_PERSONA_SESSION_FIELDS = (
    "profile_id", "session_id", "valence", "arousal", "tenderness", "possessiveness",
    "longing", "security", "protective_drive", "libido", "mood_label",
    "session_defensiveness", "residue", "inner_thought", "updated_at",
)
_PERSONA_EVENT_FIELDS = (
    "id", "profile_id", "session_id", "event_type", "mood_label", "confidence", "created_at",
)
_CANDIDATE_FIELDS = (
    "id", "title", "name", "kind", "content", "text", "summary", "date", "status",
    "created_at", "updated_at", "bucket_id", "tags", "domain", "importance",
    "valence", "arousal", "reason", "written_at", "rejected_at",
)


def _section(name: str) -> dict[str, Any]:
    value = sh.config.get(name, {}) if isinstance(sh.config, dict) else {}
    return value if isinstance(value, dict) else {}


def _repo_root() -> Path:
    configured = str(getattr(sh, "repo_root", "") or "").strip()
    return Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[2]


def _state_dir() -> Path:
    configured = str((sh.config or {}).get("state_dir", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    buckets = str((sh.config or {}).get("buckets_dir", "") or "").strip()
    if buckets:
        return Path(buckets).expanduser().resolve().parent / "state"
    return _repo_root() / "state"


def _config_value(*, section: str | None, key: str, env_names: Iterable[str] = ()) -> Any:
    """Read a trusted path setting without accepting request-supplied paths."""
    value: Any = None
    if section:
        value = _section(section).get(key)
    if value in (None, "") and isinstance(sh.config, dict):
        value = sh.config.get(key)
    if value in (None, ""):
        for env_name in env_names:
            value = os.environ.get(env_name, "")
            if value:
                break
    return value


def _configured_path(*, section: str | None, key: str, default: Path, env_names: Iterable[str] = ()) -> Path:
    value = _config_value(section=section, key=key, env_names=env_names)
    return Path(str(value)).expanduser().resolve() if value not in (None, "") else default.resolve()


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
    return result


def _path_candidates(kind: str) -> list[Path]:
    """Return configured and historical storage locations in precedence order.

    The old Haven components did not share one fallback: reminders and reflection
    and Portrait used ``dirname(buckets_dir)/state`` while Persona used
    ``state_dir or buckets_dir``. Word Map used ``buckets_dir/state``. Keep those
    semantics when no explicit path was supplied, but still discover an existing
    file created by a neighboring release.
    """
    config = sh.config if isinstance(sh.config, dict) else {}
    buckets_raw = str(config.get("buckets_dir") or "").strip()
    buckets = (
        Path(buckets_raw).expanduser().resolve()
        if buckets_raw
        else (_repo_root() / "buckets").resolve()
    )
    configured_state = str(config.get("state_dir") or "").strip()
    state = (
        Path(configured_state).expanduser().resolve()
        if configured_state
        else (buckets.parent / "state").resolve()
    )

    # The old engines intentionally had different no-state_dir fallbacks.  Keep
    # those first in the search order, then look in the neighboring layouts that
    # appeared during the v2/v3 transition.  All fallbacks are derived from the
    # trusted startup configuration; a request cannot select a storage path.
    reminder_state = state
    persona_state = (
        Path(configured_state).expanduser().resolve()
        if configured_state
        else buckets
    )
    word_map_state = (
        Path(configured_state).expanduser().resolve()
        if configured_state
        else buckets / "state"
    )

    if kind == "reminders":
        explicit = _config_value(section=None, key="reminder_db_path", env_names=("OMBRE_REMINDER_DB_PATH",))
        if explicit not in (None, ""):
            return [Path(str(explicit)).expanduser().resolve()]
        return _dedupe_paths(
            [
                reminder_state / "reminders.sqlite",
                buckets / "state" / "reminders.sqlite",
            ]
        )
    if kind == "pending":
        explicit = _config_value(
            section="reflection",
            key="daily_chat_memory_pending_path",
            env_names=("OMBRE_DAILY_CHAT_MEMORY_PENDING_PATH",),
        )
        if explicit in (None, ""):
            explicit = config.get("daily_chat_memory_pending_path")
        if explicit not in (None, ""):
            return [Path(str(explicit)).expanduser().resolve()]
        return _dedupe_paths(
            [
                state / "daily_chat_memory_candidates.json",
                buckets / "state" / "daily_chat_memory_candidates.json",
            ]
        )
    if kind == "persona":
        explicit = _config_value(
            section="persona",
            key="db_path",
            env_names=("OMBRE_PERSONA_DB_PATH",),
        )
        if explicit not in (None, ""):
            return [Path(str(explicit)).expanduser().resolve()]
        return _dedupe_paths(
            [
                persona_state / "persona_state.db",
                buckets / "state" / "persona_state.db",
            ]
        )
    if kind == "portrait":
        explicit = _config_value(
            section="portrait",
            key="state_path",
            env_names=("OMBRE_PORTRAIT_STATE_PATH",),
        )
        if explicit not in (None, ""):
            return [Path(str(explicit)).expanduser().resolve()]
        return _dedupe_paths(
            [
                state / "portrait_state.json",
                buckets / "state" / "portrait_state.json",
            ]
        )
    if kind == "dreams":
        explicit = _config_value(
            section="dream",
            key="data_dir",
            env_names=("OMBRE_DREAM_DATA_DIR", "OMBRE_DREAMS_DIR"),
        )
        if explicit not in (None, ""):
            return [Path(str(explicit)).expanduser().resolve()]
        # DreamEngine uses config.state_dir or the process working directory.
        dream_root = (
            Path(configured_state).expanduser().resolve()
            if configured_state
            else Path.cwd().expanduser().resolve()
        )
        return _dedupe_paths(
            [
                dream_root / "dreams",
                state / "dreams",
                buckets / "dreams",
            ]
        )
    if kind == "word_map":
        explicit = _config_value(
            section="word_map",
            key="db_path",
            env_names=("OMBRE_WORD_MAP_DB_PATH",),
        )
        if explicit not in (None, ""):
            return [Path(str(explicit)).expanduser().resolve()]
        return _dedupe_paths(
            [
                word_map_state / "word_map.sqlite",
                buckets / "state" / "word_map.sqlite",
            ]
        )
    raise ValueError(f"unknown compatibility storage: {kind}")


def _path_for(kind: str) -> Path:
    return _path_for_read(kind, directory=kind == "dreams")


def _path_for_read(kind: str, *, directory: bool = False) -> Path:
    candidates = _path_candidates(kind)
    for candidate in candidates:
        if (candidate.is_dir() if directory else candidate.is_file()):
            return candidate
    return candidates[0]


def _limit(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(maximum, number))


def _auth(request: Request) -> Response | None:
    return sh._require_auth(request)


def _base(status: str, *, available: bool, error: str | None = None, read_only: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "available": available, "read_only": read_only}
    if error:
        payload["error"] = error
    return payload


def _failure(error: str, *, key: str | None = None, status_code: int = 503, read_only: bool = True) -> JSONResponse:
    payload = _base("disabled", available=False, error=error, read_only=read_only)
    if key:
        payload[key] = []
    return JSONResponse(payload, status_code=status_code)


def _exception(exc: Exception, *, key: str | None = None, read_only: bool = True) -> JSONResponse:
    logger.warning("Legacy compatibility read failed: %s", exc, exc_info=True)
    if isinstance(exc, FileNotFoundError):
        return _failure("compatibility storage is unavailable", key=key, status_code=503, read_only=read_only)
    if isinstance(exc, (json.JSONDecodeError, UnicodeError, ValueError, TypeError)):
        message = "compatibility data is malformed"
    elif isinstance(exc, sqlite3.Error):
        message = "compatibility database could not be read"
    else:
        message = "compatibility data could not be read"
    return _failure(message, key=key, status_code=500, read_only=read_only)


def _write_connect(path: Path) -> sqlite3.Connection:
    """Open an existing compatibility SQLite file for narrowly validated writes.

    This deliberately never creates the database or schema; writes stay inside the
    standalone reminder store and never touch memory buckets/embeddings.
    """
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"compatibility storage is unavailable: {path.name}")
    # ``mode=rw`` prevents a race between the existence check and connect from
    # silently creating a new database through a legacy write endpoint.
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _existing_sqlite_path(kind: str) -> Path:
    """Pick an existing component store without creating a compatibility file."""
    return _path_for_read(kind)


def _readonly_connect(path: Path) -> sqlite3.Connection:
    """Open an existing SQLite file without allowing creation or writes."""
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"compatibility storage is unavailable: {path.name}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(conn, name):
        return set()
    quoted = '"' + str(name).replace('"', '""') + '"'
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({quoted})").fetchall()
        if row[1]
    }


def _find_table(conn: sqlite3.Connection, names: Iterable[str]) -> str | None:
    available = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for name in names:
        if name in available:
            return name
    return None


def _column(columns: set[str], *names: str) -> str | None:
    for name in names:
        if name in columns:
            return name
    lowered = {name.lower(): name for name in columns}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _list_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\n，、]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]
    result: list[str] = []
    for item in parts:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "是", "已启用"}:
        return True
    if text in {"0", "false", "no", "off", "n", "否", "未启用"}:
        return False
    return default


def _json_safe(value: Any) -> Any:
    """Make legacy values safe for JSONResponse without changing user text."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(child) for child in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [_json_safe(dict(row)) for row in conn.execute(sql, args).fetchall()]


def _project(value: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return _json_safe({key: value[key] for key in fields if key in value})


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _select_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    columns: Iterable[str],
    where: str = "",
    args: tuple[Any, ...] = (),
    order_by: str = "",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Select only columns present in a legacy table.

    Column names come from fixed internal allowlists, so this remains parameterized
    for values while tolerating schema versions that added or omitted fields.
    """
    available = _table_columns(conn, table)
    selected = [name for name in columns if name in available]
    if not selected:
        return []
    sql = f"SELECT {', '.join(_quoted(name) for name in selected)} FROM {_quoted(table)}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        # order_by is assembled only from the same fixed column allowlist by callers.
        sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += " LIMIT ?"
        args = args + (limit,)
    return _rows(conn, sql, args)


def _persona_rows(conn, table, fields, profile_id, *, session_id="", limit=1):
    columns = _table_columns(conn, table)
    # Without the profile key an old table cannot be safely scoped to this user.
    if "profile_id" not in columns or (session_id and "session_id" not in columns):
        return []
    where = "profile_id = ?"
    args = (profile_id,)
    if session_id:
        where += " AND session_id = ?"
        args += (session_id,)
    order = [f"{name} DESC" for name in ("updated_at", "created_at", "id") if name in columns]
    return _select_rows(
        conn, table, columns=fields, where=where, args=args,
        order_by=", ".join(order), limit=limit,
    )


def _word_rows(conn, tables, *, edges=False, limit=50):
    for table in tables:
        columns = _table_columns(conn, table)
        aliases = (
            {"term_a": ("term_a", "source_term", "source", "from_term"),
             "term_b": ("term_b", "target_term", "target", "to_term")}
            if edges else {"term": ("term", "word", "name")}
        )
        keys = {alias: _column(columns, *names) for alias, names in aliases.items()}
        if not all(keys.values()):
            continue
        groups = [_quoted(name) for name in keys.values()]
        projection = [f"{_quoted(name)} AS {alias}" for alias, name in keys.items()]
        kind = _column(columns, "kind")
        if not edges:
            projection.append(f"{_quoted(kind)} AS kind" if kind else "'' AS kind")
            if kind:
                groups.append(_quoted(kind))
        bucket = _column(columns, "bucket_id")
        count = _column(columns, "bucket_count")
        count_sql = f"COUNT(DISTINCT {_quoted(bucket)})" if bucket else (
            f"SUM({_quoted(count)})" if count else "COUNT(*)"
        )
        weight = _column(columns, "weight")
        updated = _column(columns, "updated_at")
        projection += [
            f"{count_sql} AS bucket_count",
            f"SUM({_quoted(weight)}) AS weight" if weight else "0 AS weight",
            f"MAX({_quoted(updated)}) AS updated_at" if updated else "'' AS updated_at",
        ]
        sql = (
            f"SELECT {', '.join(projection)} FROM {_quoted(table)} "
            f"GROUP BY {', '.join(groups)} ORDER BY weight DESC, "
            f"{', '.join(f'{key} ASC' for key in keys)} LIMIT ?"
        )
        return _rows(conn, sql, (limit,))
    return []


def _pending_item(item: Mapping[str, Any]) -> dict[str, Any]:
    candidate = item.get("candidate")
    safe = _project(candidate, _CANDIDATE_FIELDS) if isinstance(candidate, Mapping) else {}
    # Old records wrap the candidate; the Care list expects its display fields
    # at the top level. The envelope remains authoritative for status and ids.
    result = {**safe, **_project(item, _CANDIDATE_FIELDS)}
    if isinstance(candidate, Mapping):
        result["candidate"] = safe
    return result


def _portrait_state(state: Mapping[str, Any]) -> dict[str, Any]:
    result = _project(state, (
        "enabled", "auto_enabled", "updated_at", "last_run_date", "current_focus", "focus",
    ))
    for field in ("current_focus_items", "recent_activities", "recent_timeline"):
        if isinstance(state.get(field), list):
            result[field] = [
                _project(item, ("text", "content", "title", "name", "date", "updated_at"))
                if isinstance(item, Mapping) else item
                for item in state[field][:8] if isinstance(item, (Mapping, str))
            ]
    portrait = state.get("portrait")
    result["portrait"] = {
        scope: _project(block, ("stable", "mid_term", "stable_locked", "stable_revision", "updated_at"))
        for scope in ("user", "persona", "relationship")
        if isinstance(portrait, Mapping) and isinstance(block := portrait.get(scope), Mapping)
    }
    return result


def _portrait_gateway_url() -> str:
    admin_url = os.environ.get("OMBRE_GATEWAY_ADMIN_URL", "").strip()
    token = os.environ.get("OMBRE_GATEWAY_TOKEN", "").strip()
    if not admin_url or not token:
        return ""
    parts = urlsplit(admin_url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, "/api/portrait/initialize", "", ""))


async def _portrait_initialization(request: Request) -> Response:
    if (err := _auth(request)):
        return err
    url = _portrait_gateway_url()
    if not url:
        return JSONResponse({"error": "Portrait initialization is unavailable"}, status_code=503)
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if body != {}:
            return JSONResponse({"error": "initialization takes no options"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            response = await client.request(
                request.method, url,
                headers={"Authorization": "Bearer " + os.environ["OMBRE_GATEWAY_TOKEN"]},
                **({"json": {}} if request.method == "POST" else {}),
            )
        if response.status_code not in {200, 202}:
            raise ValueError("gateway rejected initialization request")
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise ValueError("invalid initialization response")
        return JSONResponse(_project(result, ("status", "reason", "date")), status_code=response.status_code)
    except Exception as exc:
        logger.warning("Portrait initialization gateway failed: %s", type(exc).__name__)
        return JSONResponse({"error": "Portrait initialization service is unavailable"}, status_code=503)


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"compatibility storage is unavailable: {path.name}")
    return _json_safe(json.loads(path.read_text(encoding="utf-8-sig")))


def _tags(meta: dict[str, Any]) -> set[str]:
    raw = meta.get("tags", [])
    return {item.lower() for item in _list_values(raw)}


def _metadata(bucket: dict[str, Any]) -> dict[str, Any]:
    meta = bucket.get("metadata")
    if not isinstance(meta, Mapping):
        meta = bucket.get("meta")
    if isinstance(meta, Mapping):
        return dict(meta)
    # A few early exports flattened metadata into the bucket object. Keep only
    # known metadata keys so content/id/path bookkeeping does not become a tag.
    keys = {
        "name", "type", "domain", "tags", "facets", "keywords", "subject",
        "predicate", "object", "profile_kind", "kind", "fact", "confidence",
        "source", "state", "active", "deprecated", "resolved", "created",
        "created_at", "updated_at", "updated_at", "last_active", "date",
        "event_date", "period", "valence", "arousal", "meaning", "evidence",
        "evidence_bucket_id", "evidence_moment_id", "source_bucket_id",
        "source_moment_id",
    }
    return {key: bucket[key] for key in keys if key in bucket}


def _bucket_time(bucket: dict[str, Any]) -> str:
    meta = _metadata(bucket)
    return str(
        meta.get("updated_at")
        or meta.get("last_active")
        or meta.get("created_at")
        or meta.get("created")
        or bucket.get("updated_at")
        or bucket.get("created_at")
        or ""
    )


def _profile_kind(meta: dict[str, Any], tags: set[str]) -> str:
    kind = str(meta.get("profile_kind") or meta.get("kind") or "").strip()
    if kind:
        return kind
    for tag in sorted(tags):
        if tag.startswith("profile_") and tag != "profile_fact":
            return tag.removeprefix("profile_") or "profile_fact"
    return "profile_fact"


def _profile_fact(bucket: dict[str, Any]) -> dict[str, Any] | None:
    meta = _metadata(bucket)
    tags = _tags(meta)
    domains = {item.lower() for item in _list_values(meta.get("domain", []))}
    types = {item.lower() for item in _list_values(meta.get("type", ""))}
    is_fact = (
        bool(types & {"profile_fact", "profile", "profile-fact"})
        or "profile_fact" in tags
        or "profile" in tags
        or "profile" in domains
        or "profile_fact" in domains
        or "profile-fact" in domains
    )
    if not is_fact:
        return None
    content = str(bucket.get("content") or "")
    deprecated = _as_bool(meta.get("deprecated"), False)
    resolved = _as_bool(meta.get("resolved"), False)
    active_value = meta.get("active")
    active = not deprecated and not resolved and not (
        active_value is not None and not _as_bool(active_value, True)
    )
    state = str(meta.get("state") or "").strip().lower()
    if state in {"deprecated", "inactive", "resolved", "archived"}:
        active = False
    if not state:
        state = "active" if active else ("deprecated" if deprecated else "inactive")
    kind = _profile_kind(meta, tags)
    return {
        "id": str(bucket.get("id") or ""),
        "name": str(meta.get("name") or bucket.get("id") or ""),
        "fact": str(meta.get("fact") or content),
        "content": content,
        "kind": kind,
        "profile_kind": kind,
        "subject": meta.get("subject", ""),
        "predicate": meta.get("predicate", ""),
        "object": meta.get("object", ""),
        "confidence": meta.get("confidence"),
        "source": meta.get("source", "profile_fact"),
        "state": state,
        "active": active,
        "deprecated": deprecated,
        "tags": sorted(tags),
        "created": meta.get("created_at") or meta.get("created", ""),
        "updated_at": _bucket_time(bucket),
    }


def _moment(bucket: dict[str, Any]) -> dict[str, Any] | None:
    meta = _metadata(bucket)
    tags = _tags(meta)
    bucket_id = str(bucket.get("id") or "")
    kinds = {item.lower() for item in _list_values(meta.get("type", ""))}
    domains = {item.lower() for item in _list_values(meta.get("domain", ""))}
    if not (
        bucket_id.lower().startswith("reflection_daily_")
        or bool(kinds & {"daily_impression", "daily_moment", "daily-impression", "daily-moment"})
        or {"daily_impression", "daily_moment"} & tags
        or {"reflection_daily", "daily_impression", "daily_moment", "daily-impression", "daily-moment", "日印象"} & domains
    ):
        return None
    text = str(bucket.get("content") or "")
    moment_type = meta.get("type")
    if isinstance(moment_type, (list, tuple, set)):
        moment_type = next(iter(moment_type), "daily_impression")
    moment_type = str(moment_type or "daily_impression")
    domain_value = meta.get("domain", "")
    if domain_value in (None, "") and domains:
        domain_value = next(iter(domains))
    created_at = (
        meta.get("created_at")
        or meta.get("created")
        or meta.get("date")
        or meta.get("event_date")
        or bucket.get("created_at")
        or ""
    )
    return {
        "id": bucket_id,
        "bucket_id": bucket_id,
        "name": str(meta.get("name") or bucket_id),
        "content": text,
        "text": text,
        "content_preview": " ".join(text.split())[:240],
        "type": moment_type,
        "domain": domain_value,
        "tags": sorted(tags),
        "created_at": created_at,
        "updated_at": _bucket_time(bucket),
        "date": meta.get("date") or meta.get("event_date") or created_at,
        "period": meta.get("period", ""),
        "event_date": meta.get("event_date") or meta.get("date") or "",
        "valence": meta.get("valence"),
        "arousal": meta.get("arousal"),
    }


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    normalized = str(text or "").lstrip("\ufeff")
    opening = re.match(r"^---[ \t]*(?:\r?\n|$)", normalized)
    if not opening:
        return {}, normalized.strip()
    closing = re.search(r"(?m)^---[ \t]*(?:\r?\n|$)", normalized[opening.end():])
    if not closing:
        return {}, normalized.strip()
    metadata_text = normalized[opening.end() : opening.end() + closing.start()]
    body_start = opening.end() + closing.end()
    try:
        loaded = yaml.safe_load(metadata_text) or {}
    except yaml.YAMLError:
        # A malformed record must remain readable as a body; callers can show it
        # as an unavailable record instead of turning the whole list into 500.
        return {}, normalized[body_start:].strip()
    if not isinstance(loaded, Mapping):
        loaded = {}
    return _json_safe(dict(loaded)), normalized[body_start:].strip()


def _safe_basename(value: Any, *, max_length: int = 160) -> str:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > max_length:
        return ""
    if candidate in {".", ".."} or "\x00" in candidate:
        return ""
    if Path(candidate).name != candidate or "/" in candidate or "\\" in candidate:
        return ""
    return candidate


def _safe_direct_child(root: Path, filename: str) -> Path | None:
    """Resolve one direct child and reject symlinks/containment escapes."""
    root_resolved = root.expanduser().resolve()
    if not root_resolved.is_dir():
        return None
    name = _safe_basename(filename)
    if not name:
        return None
    path = root_resolved / name
    try:
        if path.is_symlink() or not path.is_file():
            return None
        resolved = path.resolve()
    except OSError:
        return None
    if resolved.parent != root_resolved:
        return None
    return resolved


def _event_status(event: Mapping[str, Any]) -> str:
    kind = str(event.get("event") or event.get("status") or "").strip().lower()
    if kind in {"surfaced", "injected", "surface"}:
        return "surfaced"
    if kind in {"deleted", "forgotten", "expired"}:
        return "forgotten"
    return ""


def _dream_rows(root: Path) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    event_status: dict[str, str] = {}
    events_path = root / "logs" / "events.jsonl"
    if events_path.is_file():
        try:
            event_lines = events_path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            logger.warning("Skipping unreadable dream event log %s: %s", events_path.name, exc)
            event_lines = []
        for line in event_lines:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, Mapping) or not event.get("dream_id"):
                continue
            dream_id = _safe_basename(event["dream_id"])
            if not dream_id:
                continue
            item = entries.setdefault(dream_id, {"dream_id": dream_id, "status": "latent", "has_body": False})
            item["generated_at"] = event.get("generated_at", item.get("generated_at", ""))
            item["local_date"] = event.get("local_date", item.get("local_date", ""))
            item["ai_name"] = event.get("ai_name", item.get("ai_name", "AI"))
            status = _event_status(event)
            if status:
                event_status[dream_id] = status
                item["status"] = status
    if root.is_dir():
        for path in sorted(root.glob("dream_*.md")):
            if path.is_symlink():
                logger.warning("Skipping symlinked dream record %s", path.name)
                continue
            safe_path = _safe_direct_child(root, path.name)
            if safe_path is None:
                continue
            try:
                meta, _body = _parse_frontmatter(safe_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError) as exc:
                logger.warning("Skipping unreadable dream record %s: %s", path.name, exc)
                continue
            dream_id = _safe_basename(meta.get("dream_id") or path.stem) or path.stem
            item = entries.get(dream_id, {})
            file_status = "surfaced" if _as_bool(meta.get("surfaced"), False) else "latent"
            status = event_status.get(dream_id, file_status)
            if status == "forgotten" and file_status == "surfaced":
                status = file_status
            entries[dream_id] = {
                "dream_id": dream_id,
                "generated_at": meta.get("generated_at") or item.get("generated_at", ""),
                "local_date": meta.get("local_date") or item.get("local_date", ""),
                "ai_name": meta.get("ai_name") or item.get("ai_name") or "AI",
                "status": status,
                "has_body": True,
            }
    return sorted(entries.values(), key=lambda item: str(item.get("generated_at") or ""), reverse=True)


def register(mcp) -> None:
    @mcp.custom_route("/api/reminders", methods=["POST"])
    async def api_reminder_create(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        conn = None
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return JSONResponse(_base("error", available=True, error="request body must be an object", read_only=False), status_code=400)
            title = str(body.get("title") or "").strip()
            content = str(body.get("content") or body.get("text") or "").strip()
            if not title or not content:
                return JSONResponse(_base("error", available=True, error="title and content are required", read_only=False), status_code=400)
            repeat = str(body.get("repeat_rule") or "every_n_rounds").strip().lower()
            if repeat not in {"once", "none", "every_n_rounds", "daily", "morning_evening"}:
                return JSONResponse(_base("error", available=True, error="invalid repeat_rule", read_only=False), status_code=400)
            def integer(name: str, default: int, minimum: int = 0, maximum: int = 1000000) -> int:
                try:
                    value = int(body.get(name, default))
                except (TypeError, ValueError):
                    value = default
                return max(minimum, min(maximum, value))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            item_id = uuid.uuid4().hex[:16]
            values = {
                "id": item_id, "title": title, "content": content, "status": "active",
                "source": str(body.get("source") or "manual").strip() or "manual",
                "channel": str(body.get("channel") or "global").strip() or "global",
                "session_id": str(body.get("session_id") or "").strip(),
                "start_at": str(body.get("start_at") or "").strip(),
                "end_at": str(body.get("end_at") or "").strip(),
                "next_due_at": str(body.get("next_due_at") or "").strip(),
                "repeat_rule": repeat,
                "interval_rounds": integer("interval_rounds", 6, 1 if repeat == "every_n_rounds" else 0),
                "cooldown_minutes": integer("cooldown_minutes", 0),
                "daily_limit": integer("daily_limit", 1),
                "daily_reminder_date": "", "daily_reminder_count": 0,
                "max_injections": integer("max_injections", 0),
                "last_reminded_at": None, "last_reminded_round": 0, "reminder_count": 0,
                "created_at": now, "updated_at": now, "resolved_at": None,
            }
            conn = _write_connect(_path_for("reminders"))
            if not _table_exists(conn, "reminders"):
                return _failure("reminders table is unavailable", key="reminders", read_only=False)
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(reminders)").fetchall()}
            values = {key: value for key, value in values.items() if key in columns}
            if not {"id", "title", "content"} <= columns:
                return _failure("reminders table has no writable identity", key="reminders", read_only=False)
            if not values:
                return _failure("reminders table has no writable columns", key="reminders", read_only=False)
            names = list(values)
            with conn:
                conn.execute(
                    f"INSERT INTO reminders ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                    [values[name] for name in names],
                )
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (item_id,)).fetchone()
            return JSONResponse({**_base("created", available=True, read_only=False), "reminder": _project(dict(row) if row else values, _REMINDER_FIELDS)}, status_code=201)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse(_base("error", available=True, error=str(exc) or "invalid request", read_only=False), status_code=400)
        except Exception as exc:
            return _exception(exc, key="reminders", read_only=False)
        finally:
            if conn is not None:
                conn.close()

    @mcp.custom_route("/api/reminders/{reminder_id}", methods=["PATCH"])
    async def api_reminder_update(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        reminder_id = str(request.path_params.get("reminder_id") or "").strip()
        if not _SAFE_ID.fullmatch(reminder_id):
            return JSONResponse(_base("error", available=True, error="invalid reminder id", read_only=False), status_code=400)
        conn = None
        try:
            body = await request.json()
            if not isinstance(body, dict):
                return JSONResponse(_base("error", available=True, error="request body must be an object", read_only=False), status_code=400)
            conn = _write_connect(_path_for("reminders"))
            if not _table_exists(conn, "reminders"):
                return _failure("reminders table is unavailable", key="reminders", read_only=False)
            if "id" not in _table_columns(conn, "reminders"):
                return _failure("reminders table has no writable identity", key="reminders", read_only=False)
            existing = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            if existing is None:
                return JSONResponse(_base("not_found", available=True, error="reminder not found", read_only=False), status_code=404)
            updates: dict[str, Any] = {}
            for key in ("title", "content", "start_at", "end_at", "next_due_at", "channel", "session_id", "repeat_rule"):
                if key in body:
                    value = str(body.get(key) or "").strip()
                    if key in {"title", "content"} and not value:
                        return JSONResponse(_base("error", available=True, error=f"{key} is required", read_only=False), status_code=400)
                    if key == "repeat_rule" and value not in {"once", "none", "every_n_rounds", "daily", "morning_evening"}:
                        return JSONResponse(_base("error", available=True, error="invalid repeat_rule", read_only=False), status_code=400)
                    updates[key] = value
            if "status" in body:
                status = str(body.get("status") or "").strip().lower()
                if status not in {"active", "done", "archived"}:
                    return JSONResponse(_base("error", available=True, error="invalid reminder status", read_only=False), status_code=400)
                updates["status"] = status
                updates["resolved_at"] = None if status == "active" else datetime.now(timezone.utc).isoformat(timespec="seconds")
            for key in ("interval_rounds", "cooldown_minutes", "daily_limit", "max_injections"):
                if key in body:
                    try:
                        updates[key] = max(0, min(1000000, int(body.get(key))))
                    except (TypeError, ValueError):
                        return JSONResponse(_base("error", available=True, error=f"invalid {key}", read_only=False), status_code=400)
            if "snooze_minutes" in body:
                try:
                    minutes = max(1, min(525600, int(body.get("snooze_minutes"))))
                except (TypeError, ValueError):
                    return JSONResponse(_base("error", available=True, error="invalid snooze_minutes", read_only=False), status_code=400)
                updates["next_due_at"] = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")
                updates["status"] = "active"
                updates["resolved_at"] = None
            if not updates:
                return JSONResponse({**_base("ok", available=True, read_only=False), "reminder": _project(dict(existing), _REMINDER_FIELDS)})
            updates["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(reminders)").fetchall()}
            updates = {key: value for key, value in updates.items() if key in columns}
            if not updates:
                return JSONResponse({**_base("ok", available=True, read_only=False), "reminder": _project(dict(existing), _REMINDER_FIELDS)})
            assignments = ", ".join(f"{key} = ?" for key in updates)
            with conn:
                conn.execute(f"UPDATE reminders SET {assignments} WHERE id = ?", [*updates.values(), reminder_id])
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            return JSONResponse({**_base("updated", available=True, read_only=False), "reminder": _project(dict(row), _REMINDER_FIELDS) if row else {}})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return JSONResponse(_base("error", available=True, error=str(exc) or "invalid request", read_only=False), status_code=400)
        except Exception as exc:
            return _exception(exc, key="reminders", read_only=False)
        finally:
            if conn is not None:
                conn.close()

    @mcp.custom_route("/api/reminders", methods=["GET"])
    async def api_reminders(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        status = str(request.query_params.get("status") or "active").strip().lower()
        if status not in {"active", "done", "archived", "all"}:
            return JSONResponse(_base("error", available=True, error="invalid reminder status"), status_code=400)
        conn = None
        try:
            conn = _readonly_connect(_existing_sqlite_path("reminders"))
            if not _table_exists(conn, "reminders"):
                return _failure("reminders table is unavailable", key="reminders")
            columns = _table_columns(conn, "reminders")
            projection = [name for name in _REMINDER_FIELDS if name in columns]
            if not projection:
                return _failure("reminders table has no readable columns", key="reminders")
            where = ""
            args: tuple[Any, ...] = ()
            status_column = _column(columns, "status")
            if status != "all" and status_column:
                where = f"{status_column} = ?"
                args = (status,)
            order_candidates = [name for name in ("next_due_at", "updated_at", "created_at") if name in columns]
            order_by = ", ".join(f"{name} ASC" for name in order_candidates) or projection[0]
            sql = f"SELECT {', '.join(projection)} FROM reminders"
            if where:
                sql += f" WHERE {where}"
            sql += f" ORDER BY {order_by} LIMIT ?"
            rows = _rows(conn, sql, args + (_limit(request.query_params.get("limit"), 50, 200),))
            return JSONResponse({**_base("ok", available=True), "count": len(rows), "reminders": rows})
        except Exception as exc:
            return _exception(exc, key="reminders")
        finally:
            if conn is not None:
                conn.close()

    @mcp.custom_route("/api/moments", methods=["GET"])
    async def api_moments(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        manager = getattr(sh, "bucket_mgr", None)
        if manager is None:
            return _failure("v3 bucket manager is not injected", key="moments")
        try:
            bucket_id = str(request.query_params.get("bucket_id") or "").strip()
            if bucket_id and not _SAFE_ID.fullmatch(bucket_id):
                return JSONResponse(_base("error", available=True, error="invalid bucket_id"), status_code=400)
            buckets = await manager.list_all(include_archive=False)
            rows = [row for bucket in buckets if (not bucket_id or str(bucket.get("id")) == bucket_id) and (row := _moment(bucket))]
            rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
            rows = rows[:_limit(request.query_params.get("limit"), 20, 200)]
            return JSONResponse({**_base("ok", available=True), "count": len(rows), "moments": _json_safe(rows)})
        except Exception as exc:
            return _exception(exc, key="moments")

    @mcp.custom_route("/api/daily-chat-memory/pending", methods=["GET"])
    async def api_daily_chat_memory_pending(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        wanted = str(request.query_params.get("status") or "pending").strip()
        try:
            path = _path_for("pending")
            # Reflection creates this file lazily when it first stores candidates.
            # A missing parent still indicates unavailable storage, not an empty queue.
            if not path.exists() and path.parent.is_dir():
                return JSONResponse({**_base("empty", available=True), "storage_state": "not_created", "count": 0, "items": []})
            raw = _read_json(path)
            if isinstance(raw, dict):
                items = raw.get("items", raw.get("candidates", []))
            else:
                items = raw
            if not isinstance(items, list):
                raise ValueError("pending storage must contain a list")
            if wanted and wanted != "all":
                items = [item for item in items if isinstance(item, dict) and str(item.get("status") or "") == wanted]
            items = [item for item in items if isinstance(item, dict)]
            items.sort(key=lambda item: str(item.get("created_at") or item.get("updated_at") or ""), reverse=True)
            items = items[:_limit(request.query_params.get("limit"), 50, 200)]
            return JSONResponse({**_base("ok", available=True), "count": len(items), "items": [_pending_item(item) for item in items]})
        except Exception as exc:
            return _exception(exc, key="items")

    @mcp.custom_route("/api/persona", methods=["GET"])
    async def api_persona(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        conn = None
        try:
            conn = _readonly_connect(_path_for("persona"))
            if "profile_id" not in _table_columns(conn, "persona_global_state"):
                return _failure("persona global state is unavailable", key="sessions")
            # Match PersonaEngine's historical default so an old database opens
            # even when persona.profile_id was omitted from the runtime config.
            profile_id = str(_section("persona").get("profile_id") or "haven_xiaoyu")
            global_rows = _persona_rows(conn, "persona_global_state", _PERSONA_STATE_FIELDS, profile_id)
            session_limit = _limit(request.query_params.get("sessions_limit"), 20, 100)
            event_limit = _limit(request.query_params.get("events_limit"), 20, 100)
            session_id = str(request.query_params.get("session_id") or "").strip()
            sessions = _persona_rows(
                conn, "persona_session_state", _PERSONA_SESSION_FIELDS, profile_id,
                session_id=session_id, limit=session_limit,
            )
            events = _persona_rows(
                conn, "persona_events", _PERSONA_EVENT_FIELDS, profile_id,
                session_id=session_id, limit=event_limit,
            )
            state = global_rows[0] if global_rows else {}
            return JSONResponse({**_base("ok", available=True), "profile_id": profile_id, "state": state, "sessions": sessions, "events": events, "config": {"enabled": bool(_section("persona").get("enabled", True)), "mode": _section("persona").get("mode", ""), "model": _section("persona").get("model", ""), "api_ready": bool(_section("persona").get("api_key"))}})
        except Exception as exc:
            return _exception(exc, key="sessions")
        finally:
            if conn is not None:
                conn.close()

    @mcp.custom_route("/api/dreams", methods=["GET"])
    async def api_dreams(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        try:
            rows = _dream_rows(_path_for("dreams"))[:_limit(request.query_params.get("limit"), 30, 100)]
            cfg = _section("dream")
            return JSONResponse({**_base("ok", available=True), "enabled": bool(cfg.get("enabled", True)), "auto_enabled": bool(cfg.get("auto_enabled", True)), "surface_enabled": bool(cfg.get("surface_enabled", True)), "records": rows})
        except Exception as exc:
            return _exception(exc, key="records")

    @mcp.custom_route("/api/dreams/{dream_id}", methods=["GET"])
    async def api_dream_detail(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        dream_id = str(request.path_params.get("dream_id") or "").strip()
        if not _SAFE_ID.fullmatch(dream_id) or not _safe_basename(dream_id):
            return JSONResponse(_base("not_found", available=True, error="dream body unavailable"), status_code=404)
        try:
            root = _path_for("dreams")
            path = _safe_direct_child(root, f"{dream_id}.md")
            if path is None:
                return JSONResponse(_base("not_found", available=True, error="dream body unavailable"), status_code=404)
            meta, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
            status = "surfaced" if _as_bool(meta.get("surfaced"), False) else "latent"
            return JSONResponse({**_base("ok", available=True), "dream_id": dream_id, "generated_at": meta.get("generated_at", ""), "local_date": meta.get("local_date", ""), "ai_name": meta.get("ai_name") or "AI", "dream_status": status, "status_value": status, "body": body})
        except Exception as exc:
            return _exception(exc)

    @mcp.custom_route("/api/portrait-state", methods=["GET"])
    async def api_portrait(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        try:
            path = _path_for("portrait")
            state = {} if not path.exists() and path.parent.is_dir() and _portrait_gateway_url() else _read_json(path)
            if not isinstance(state, dict):
                raise ValueError("portrait state must be an object")
            initialized = bool(state.get("runs"))
            state = _portrait_state(state)
            return JSONResponse({**state, **_base("ok", available=True), "state": state, "portrait": state.get("portrait", {}),
                                 "initialized": initialized, "initialization_available": bool(_portrait_gateway_url())})
        except Exception as exc:
            return _exception(exc, key="state")

    mcp.custom_route("/api/portrait/initialize", methods=["GET"])(_portrait_initialization)
    mcp.custom_route("/api/portrait/initialize", methods=["POST"])(_portrait_initialization)

    @mcp.custom_route("/api/profile-facts", methods=["GET"])
    async def api_profile_facts(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        manager = getattr(sh, "bucket_mgr", None)
        if manager is None:
            return _failure("v3 bucket manager is not injected", key="facts")
        try:
            buckets = await manager.list_all(include_archive=True)
            rows = [row for bucket in buckets if (row := _profile_fact(bucket))]
            rows.sort(key=lambda item: str(item.get("updated_at") or item.get("created") or ""), reverse=True)
            rows = rows[:_limit(request.query_params.get("limit"), 100, 500)]
            return JSONResponse({**_base("ok", available=True), "count": len(rows), "facts": _json_safe(rows)})
        except Exception as exc:
            return _exception(exc, key="facts")

    @mcp.custom_route("/api/word-map", methods=["GET"])
    async def api_word_map(request: Request) -> Response:
        if (err := _auth(request)):
            return err
        conn = None
        try:
            conn = _readonly_connect(_path_for("word_map"))
            nodes_limit = _limit(request.query_params.get("nodes"), 50, 500)
            edges_limit = _limit(request.query_params.get("edges"), 50, 500)
            nodes = _word_rows(conn, ("word_card_nodes", "word_nodes"), limit=nodes_limit)
            edges = _word_rows(conn, ("word_edges",), edges=True, limit=edges_limit)
            return JSONResponse({**_base("ok", available=True), "enabled": bool(_section("word_map").get("enabled", False)), "stats": {"node_count": len(nodes), "edge_count": len(edges)}, "nodes": nodes, "edges": edges})
        except Exception as exc:
            return _exception(exc, key="nodes")
        finally:
            if conn is not None:
                conn.close()
