"""Private HTTP adapter between Haven Gateway and Xinchao.

The adapter is deliberately isolated from gateway routing logic. Context reads
fail closed, delivery goes through a durable SQLite outbox, and no token or
conversation body is written to diagnostics.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

try:
    from errors import safe_error_detail
except ImportError:  # pragma: no cover - tests import the full package
    def safe_error_detail(exc: BaseException) -> str:
        return str(exc)[:200]


logger = logging.getLogger("ombre_brain.xinchao_adapter")

_DEFAULT_CONTEXT_MAX_TOKENS = 600
_HARD_CONTEXT_MAX_TOKENS = 4000
_DEFAULT_READ_TIMEOUT_SECONDS = 1.5
_DEFAULT_WRITE_TIMEOUT_SECONDS = 2.0
_DEFAULT_CONTINUITY_LIMIT = 6
_MAX_CONTINUITY_LIMIT = 24
_DEFAULT_SESSION_ID_MAX_LENGTH = 160
_DEFAULT_OUTBOX_MAX_ATTEMPTS = 8
_DEFAULT_OUTBOX_MAX_AGE_HOURS = 72
_DEFAULT_OUTBOX_POLL_SECONDS = 5.0
_DEFAULT_RETRY_BASE_SECONDS = 5.0
_DEFAULT_RETRY_MAX_SECONDS = 300.0
_LEASE_SECONDS = 60.0

_ALLOWED_SECTION_IDS = {
    "dynamic_state",
    "handoff_notes",
    "cross_client_recent",
    "recent_continuity",
}
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 405, 413, 422}
_SERVER_RETRY_STATUS_CODES = set(range(500, 600))
_DYNAMIC_TONES = {
    "focused",
    "scattered",
    "reflective",
    "playful",
    "tired",
    "tender",
    "neutral",
}
_PRODUCTION_HOST_MARKERS = {"ombre-brain", "xinchao-nian-caric"}


@dataclass(frozen=True)
class XinchaoContext:
    text: str
    estimated_tokens: int
    section_count: int
    degraded: bool
    warnings: tuple[str, ...]


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _normalize_visible_text(text: Any) -> str:
    return unicodedata.normalize("NFC", str(text or "").replace("\r\n", "\n").replace("\r", "\n")).strip()


def _normalized_text_hash(text: Any) -> str:
    return hashlib.sha256(_normalize_visible_text(text).encode("utf-8")).hexdigest()


def _compact_error(exc: BaseException) -> tuple[str, str]:
    detail = safe_error_detail(exc)
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "timeout", detail
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}", detail
    if isinstance(exc, (httpx.TransportError, OSError)):
        return "network", detail
    return "unexpected", detail


class XinchaoOutbox:
    """Durable write-behind queue for Xinchao HTTP delivery."""

    def __init__(
        self,
        db_path: str,
        *,
        max_attempts: int = _DEFAULT_OUTBOX_MAX_ATTEMPTS,
        max_age_hours: float = _DEFAULT_OUTBOX_MAX_AGE_HOURS,
        retry_base_seconds: float = _DEFAULT_RETRY_BASE_SECONDS,
        retry_max_seconds: float = _DEFAULT_RETRY_MAX_SECONDS,
    ) -> None:
        self.db_path = db_path
        self.max_attempts = max(1, int(max_attempts))
        self.max_age_seconds = max(0.0, float(max_age_hours) * 3600.0)
        self.retry_base_seconds = max(0.1, float(retry_base_seconds))
        self.retry_max_seconds = max(self.retry_base_seconds, float(retry_max_seconds))
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _init_db(self) -> None:
        connection = self._connect()
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS xinchao_outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_key TEXT NOT NULL UNIQUE,
              kind TEXT NOT NULL CHECK(kind IN ('continuity','conversation_event')),
              payload_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              delivered_at TEXT NOT NULL DEFAULT '',
              lease_expires_at TEXT NOT NULL DEFAULT '',
              last_error_code TEXT NOT NULL DEFAULT '',
              last_error_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_xinchao_outbox_claim
            ON xinchao_outbox (status, next_attempt_at, id)
            """
        )
        connection.commit()
        connection.close()

    @staticmethod
    def _iso(value: datetime | None = None) -> str:
        return (value or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    @staticmethod
    def _parse_iso(value: Any) -> datetime:
        try:
            return datetime.fromisoformat(str(value or ""))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    def enqueue(self, item: dict[str, Any], *, now: datetime | None = None) -> str:
        event_key = str(item.get("event_key") or "").strip()
        kind = str(item.get("kind") or "").strip()
        payload = item.get("payload")
        if not event_key or kind not in {"continuity", "conversation_event"} or not isinstance(payload, dict):
            raise ValueError("invalid xinchao outbox item")
        created_at = self._iso(now)
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT OR IGNORE INTO xinchao_outbox
                (event_key, kind, payload_json, status, attempts, next_attempt_at, created_at)
                VALUES (?, ?, ?, 'pending', 0, ?, ?)
                """,
                (event_key, kind, json.dumps(payload, ensure_ascii=False), created_at, created_at),
            )
            connection.commit()
            row = connection.execute(
                "SELECT status FROM xinchao_outbox WHERE event_key = ?", (event_key,)
            ).fetchone()
            return str(row["status"] or "pending") if row else "missing"
        finally:
            connection.close()

    def _expire_overdue(self, connection: sqlite3.Connection, now: datetime) -> None:
        if self.max_age_seconds <= 0:
            return
        connection.execute(
            """
            UPDATE xinchao_outbox
            SET status = 'failed',
                last_error_code = 'expired',
                last_error_at = ?
            WHERE status IN ('pending', 'processing')
              AND created_at <= ?
            """,
            (self._iso(now), self._iso(now - timedelta(seconds=self.max_age_seconds))),
        )

    def claim(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        current = now or datetime.now(timezone.utc)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_overdue(connection, current)
            row = connection.execute(
                """
                SELECT * FROM xinchao_outbox
                WHERE status = 'pending'
                  AND next_attempt_at <= ?
                ORDER BY next_attempt_at, id
                LIMIT 1
                """,
                (self._iso(current),),
            ).fetchone()
            if not row:
                connection.commit()
                return None
            lease_expires_at = current + timedelta(seconds=_LEASE_SECONDS)
            updated = connection.execute(
                """
                UPDATE xinchao_outbox
                SET status = 'processing', lease_expires_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (self._iso(lease_expires_at), int(row["id"])),
            )
            if updated.rowcount != 1:
                connection.commit()
                return None
            connection.commit()
            item = dict(row)
            item["status"] = "processing"
            item["payload"] = json.loads(str(item.pop("payload_json") or "{}"))
            return item
        finally:
            connection.close()

    def _update_by_event_key(
        self,
        event_key: str,
        *,
        status: str,
        attempts_delta: int,
        error_code: str = "",
        next_attempt_at: datetime | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        connection = self._connect()
        try:
            connection.execute(
                """
                UPDATE xinchao_outbox
                SET status = ?,
                    attempts = attempts + ?,
                    delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END,
                    lease_expires_at = '',
                    last_error_code = ?,
                    last_error_at = CASE WHEN ? = '' THEN last_error_at ELSE ? END,
                    next_attempt_at = CASE WHEN ? IS NULL THEN next_attempt_at ELSE ? END
                WHERE event_key = ?
                """,
                (
                    status,
                    attempts_delta,
                    status,
                    self._iso(now) if status == "delivered" else "",
                    error_code,
                    error_code,
                    self._iso(now) if error_code else "",
                    self._iso(next_attempt_at) if next_attempt_at else None,
                    self._iso(next_attempt_at) if next_attempt_at else "",
                    event_key,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def mark_delivered(self, item: dict[str, Any]) -> None:
        self._update_by_event_key(
            str(item.get("event_key") or ""),
            status="delivered",
            attempts_delta=1,
        )

    def mark_retry(self, item: dict[str, Any], error_code: str) -> None:
        attempts = int(item.get("attempts") or 0) + 1
        delay = min(
            self.retry_max_seconds,
            self.retry_base_seconds * (2 ** max(0, attempts - 1)),
        )
        delay *= 0.9 + random.random() * 0.2
        next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=max(0.1, delay))
        self._update_by_event_key(
            str(item.get("event_key") or ""),
            status="pending",
            attempts_delta=1,
            error_code=error_code,
            next_attempt_at=next_attempt_at,
        )

    def mark_failed(self, item: dict[str, Any], error_code: str) -> None:
        self._update_by_event_key(
            str(item.get("event_key") or ""),
            status="failed",
            attempts_delta=1,
            error_code=error_code,
        )

    def _count(self, status: str) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM xinchao_outbox WHERE status = ?", (status,)
            ).fetchone()
            return int(row["count"] or 0) if row else 0
        finally:
            connection.close()

    def pending_count(self) -> int:
        return self._count("pending")

    def failed_count(self) -> int:
        return self._count("failed")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "pending": self.pending_count(),
            "processing": self._count("processing"),
            "failed": self.failed_count(),
            "delivered": self._count("delivered"),
        }


class XinchaoAdapter:
    """Bounded context reader and idempotent conversation-event publisher."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        db_path: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        raw = config.get("xinchao_adapter", {}) if isinstance(config.get("xinchao_adapter", {}), dict) else {}
        self.enabled = _bool(raw.get("enabled"), False)
        self.base_url = str(raw.get("base_url") or "").rstrip("/") if self.enabled else ""
        self.service_token_env = str(raw.get("service_token_env") or "").strip()
        self.service_token = (
            os.environ.get(self.service_token_env, "")
            if self.enabled and self.service_token_env
            else ""
        )
        if self.enabled and self.base_url:
            self._validate_base_url(self.base_url)
        self.context_max_tokens = _bounded_int(
            raw.get("context_max_tokens"),
            _DEFAULT_CONTEXT_MAX_TOKENS,
            200,
            _HARD_CONTEXT_MAX_TOKENS,
        )
        self.read_timeout_seconds = _positive_float(
            raw.get("read_timeout_seconds"), _DEFAULT_READ_TIMEOUT_SECONDS
        )
        self.write_timeout_seconds = _positive_float(
            raw.get("write_timeout_seconds"), _DEFAULT_WRITE_TIMEOUT_SECONDS
        )
        self.outbox_poll_seconds = _positive_float(
            raw.get("outbox_poll_seconds"), _DEFAULT_OUTBOX_POLL_SECONDS
        )
        self.continuity_limit = _bounded_int(
            raw.get("continuity_limit"),
            _DEFAULT_CONTINUITY_LIMIT,
            1,
            _MAX_CONTINUITY_LIMIT,
        )
        self.notify_dynamic_state = _bool(raw.get("notify_dynamic_state"), True)
        self.session_id_max_length = _bounded_int(
            raw.get("session_id_max_length"),
            _DEFAULT_SESSION_ID_MAX_LENGTH,
            64,
            512,
        )
        outbox_enabled = _bool(raw.get("outbox_enabled"), True)
        self.outbox = (
            XinchaoOutbox(
                db_path,
                max_attempts=_bounded_int(raw.get("outbox_max_attempts"), 8, 1, 64),
                max_age_hours=max(0.0, float(raw.get("outbox_max_age_hours") or 72)),
                retry_base_seconds=_positive_float(
                    raw.get("outbox_retry_base_seconds"), _DEFAULT_RETRY_BASE_SECONDS
                ),
                retry_max_seconds=_positive_float(
                    raw.get("outbox_retry_max_seconds"), _DEFAULT_RETRY_MAX_SECONDS
                ),
            )
            if self.enabled and outbox_enabled
            else None
        )
        self.http_client = http_client
        self._owns_http_client = http_client is None
        self._reachable: bool | None = None
        self._auth_degraded = False
        self._last_error_code = ""
        self._last_context_success_at = ""
        self._last_delivery_success_at = ""
        self._worker_task: asyncio.Task | None = None
        self._worker_event: asyncio.Event | None = None
        self._worker_running = False
        self._worker_loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _validate_base_url(base_url: str) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("xinchao base_url must be an explicit http(s) origin")
        host = parsed.hostname.lower()
        if any(marker in host for marker in _PRODUCTION_HOST_MARKERS):
            raise ValueError("production xinchao host is not allowed for the experimental adapter")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.service_token)

    def map_session_id(self, client_label: Any, session_id: Any) -> str:
        client = self.normalize_client_label(client_label)
        raw_session = str(session_id or "").strip()
        if not raw_session:
            session = "default"
        else:
            if any(ord(char) < 32 or ord(char) == 127 for char in raw_session):
                raise ValueError("session identity contains control characters")
            session = re.sub(r"[^A-Za-z0-9._:-]+", "-", raw_session)
        normalized = f"gateway:{client}:{session}"
        max_length = self.session_id_max_length
        if len(normalized) <= max_length:
            return normalized
        prefix_length = max(1, max_length - 25)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        return f"{normalized[:prefix_length]}:{digest}"

    def normalize_client_label(self, client_label: Any) -> str:
            raw = str(client_label or "").strip()
            if not raw:
                return "unknown"
            if any(ord(char) < 32 or ord(char) == 127 for char in raw):
                raise ValueError("session identity contains control characters")
            return re.sub(r"[^A-Za-z0-9._:-]+", "-", raw)

    def build_stable_event_id(
        self,
        profile_id: Any,
        gateway_session_id: Any,
        round_id: Any,
        role: Any,
        route: Any,
        visible_text: Any,
    ) -> str:
        text_hash = _normalized_text_hash(visible_text)
        material = "\0".join(
            [
                str(profile_id or "default"),
                str(gateway_session_id or ""),
                str(int(round_id or 0)),
                str(role or ""),
                str(route or ""),
                text_hash,
            ]
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"ombre-gw-v1:{digest}"

    def _mapped_client(self, gateway_session_id: str) -> str:
        parts = str(gateway_session_id or "").split(":")
        return parts[1] if len(parts) > 2 and parts[1] else "gateway"

    def build_continuity_outbox_item(
        self,
        *,
        profile_id: Any,
        gateway_session_id: Any,
        round_id: Any,
        route: Any,
        user_text: Any,
        assistant_text: Any,
        client_label: Any = "",
    ) -> dict[str, Any]:
        session_id = str(gateway_session_id or "")
        user = _normalize_visible_text(user_text)
        assistant = _normalize_visible_text(assistant_text)
        if not user and not assistant:
            raise ValueError("continuity outbox item requires visible text")
        messages = []
        for role, text in (("user", user), ("assistant", assistant)):
            if not text:
                continue
            turn_id = self.build_stable_event_id(
                profile_id,
                session_id,
                round_id,
                role,
                route,
                text,
            )
            messages.append({"turn_id": turn_id, "role": role, "text": text})
        client = self._mapped_client(session_id) or "gateway"
        event_key = self.build_stable_event_id(
            profile_id,
            session_id,
            round_id,
            "continuity_round",
            route,
            "\0".join([user, assistant]),
        )
        return {
            "event_key": event_key,
            "kind": "continuity",
            "payload": {
                "session_id": session_id,
                "client": client,
                "messages": messages,
                "limit": self.continuity_limit,
            },
        }

    def build_dynamic_event_outbox_item(
        self,
        *,
        profile_id: Any,
        gateway_session_id: Any,
        round_id: Any,
        route: Any,
        tone: Any,
    ) -> dict[str, Any]:
        safe_tone = str(tone or "").strip().lower()
        if safe_tone not in _DYNAMIC_TONES:
            safe_tone = "neutral"
        event_id = self.build_stable_event_id(
            profile_id,
            gateway_session_id,
            round_id,
            "round",
            route,
            "",
        )
        return {
            "event_key": event_id,
            "kind": "conversation_event",
            "payload": {
                "event_id": event_id,
                "session_id": str(gateway_session_id or ""),
                "tone": safe_tone,
            },
        }

    def _headers(self) -> dict[str, str]:
        if not self.service_token:
            raise ValueError("xinchao service token is not configured")
        return {"Authorization": f"Bearer {self.service_token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float,
    ) -> httpx.Response:
        headers = self._headers()
        url = f"{self.base_url}{path}"
        if self.http_client is not None:
            client_method = self.http_client.get if method == "GET" else self.http_client.post
            return await client_method(
                url,
                **({"params": params} if params else {}),
                **({"json": json_body} if json_body else {}),
                headers=headers,
                timeout=timeout,
            )
        async with httpx.AsyncClient(timeout=timeout) as client:
            client_method = client.get if method == "GET" else client.post
            return await client_method(
                url,
                **({"params": params} if params else {}),
                **({"json": json_body} if json_body else {}),
                headers=headers,
            )

    async def fetch_context(
        self,
        session_id: str,
        *,
        existing_turn_ids: set[str] | None = None,
        existing_texts: list[str] | None = None,
    ) -> XinchaoContext:
        if not self.enabled:
            return XinchaoContext("", 0, 0, False, ("disabled",))
        if not self.configured:
            return XinchaoContext("", 0, 0, True, ("not_configured",))
        params = {
            "session_id": session_id,
            "mode": "turn",
            "max_tokens": self.context_max_tokens,
        }
        try:
            response = await self._request(
                "GET",
                "/v1/context",
                params=params,
                timeout=self.read_timeout_seconds,
            )
        except Exception as exc:
            error_code, _detail = _compact_error(exc)
            self._record_failure(error_code, auth_sensitive=error_code in {"http_401", "http_403"})
            return XinchaoContext("", 0, 0, True, (error_code,))
        if response.status_code != 200:
            error_code = f"http_{response.status_code}"
            self._record_failure(error_code, auth_sensitive=response.status_code in {401, 403})
            return XinchaoContext("", 0, 0, True, (error_code,))
        try:
            envelope = response.json()
        except ValueError:
            self._record_failure("malformed_context")
            return XinchaoContext("", 0, 0, True, ("malformed_context",))
        context = self._parse_context_envelope(
            envelope,
            existing_turn_ids=existing_turn_ids or set(),
            existing_texts=existing_texts or [],
        )
        if context.text:
            self._reachable = True
            self._last_context_success_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._last_error_code = ""
        return context

    @staticmethod
    def _dedupe_key(value: str) -> str:
        return hashlib.sha256(" ".join(_normalize_visible_text(value).split()).encode("utf-8")).hexdigest()

    def _parse_context_envelope(
        self,
        envelope: Any,
        *,
        existing_turn_ids: set[str],
        existing_texts: list[str],
    ) -> XinchaoContext:
        if not isinstance(envelope, dict):
            return XinchaoContext("", 0, 0, True, ("malformed_context",))
        delivered = envelope.get("delivered")
        if delivered is not True:
            return XinchaoContext("", 0, 0, delivered is not False, ("not_delivered",))
        sections = envelope.get("sections")
        if not isinstance(sections, list):
            return XinchaoContext("", 0, 0, True, ("malformed_context",))
        existing_hashes = {
            self._dedupe_key(text)
            for text in (existing_texts or [])
            if str(text or "").strip()
        }
        seen_hashes = set(existing_hashes)
        selected: list[str] = []
        for section in sections:
            if not isinstance(section, dict):
                continue
            if str(section.get("id") or "") not in _ALLOWED_SECTION_IDS:
                continue
            content = _normalize_visible_text(section.get("content"))
            if not content:
                continue
            hash_key = self._dedupe_key(content)
            if hash_key in seen_hashes:
                continue
            seen_hashes.add(hash_key)
            selected.append(content)
        text = "\n\n".join(selected).strip()
        if not text:
            return XinchaoContext("", 0, 0, False, ("empty",))
        try:
            estimated_tokens = max(1, int(envelope.get("estimatedTokens") or 0))
        except (TypeError, ValueError):
            estimated_tokens = max(1, len(text) // 3)
        return XinchaoContext(
            text=text,
            estimated_tokens=estimated_tokens,
            section_count=len(selected),
            degraded=False,
            warnings=(),
        )

    def _record_failure(self, error_code: str, *, auth_sensitive: bool = False) -> None:
        self._reachable = False
        self._last_error_code = error_code
        if auth_sensitive:
            self._auth_degraded = True

    async def deliver(self, item: dict[str, Any]) -> None:
        if not self.enabled or not self.configured:
            raise ValueError("xinchao adapter is not configured")
        kind = str(item.get("kind") or "")
        payload = item.get("payload")
        if kind == "continuity":
            path = "/v1/continuity/sync"
        elif kind == "conversation_event":
            path = "/v1/conversation-event"
        else:
            raise ValueError("invalid xinchao outbox kind")
        try:
            response = await self._request(
                "POST",
                path,
                json_body=payload if isinstance(payload, dict) else {},
                timeout=self.write_timeout_seconds,
            )
        except Exception as exc:
            error_code, _detail = _compact_error(exc)
            if self.outbox:
                self.outbox.mark_retry(item, error_code)
            self._record_failure(error_code)
            return
        if 200 <= response.status_code < 300 or response.status_code == 409:
            if self.outbox:
                self.outbox.mark_delivered(item)
            self._reachable = True
            self._auth_degraded = False
            self._last_delivery_success_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._last_error_code = ""
            return
        error_code = f"http_{response.status_code}"
        if self.outbox:
            if (
                response.status_code in _NON_RETRYABLE_STATUS_CODES
                or int(item.get("attempts") or 0) + 1 >= self.outbox.max_attempts
            ):
                self.outbox.mark_failed(item, error_code)
            else:
                self.outbox.mark_retry(item, error_code)
        self._record_failure(error_code, auth_sensitive=response.status_code in {401, 403})

    def _wake_worker(self) -> None:
        if self._worker_event and self._worker_loop:
            self._worker_loop.call_soon_threadsafe(self._worker_event.set)

    async def start_delivery_worker(self) -> bool:
        if not self.enabled or not self.outbox or self._worker_running:
            return False
        self._worker_running = True
        self._worker_loop = asyncio.get_running_loop()
        self._worker_event = asyncio.Event()
        self._worker_task = asyncio.create_task(self._delivery_worker(), name="ombre-xinchao-outbox")
        self._wake_worker()
        return True

    async def stop_delivery_worker(self) -> None:
        self._worker_running = False
        self._wake_worker()
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        self._worker_event = None
        self._worker_loop = None

    async def _delivery_worker(self) -> None:
        poll_seconds = self.outbox_poll_seconds if hasattr(self, "outbox_poll_seconds") else _DEFAULT_OUTBOX_POLL_SECONDS
        while self._worker_running:
            item = None
            if self.outbox:
                try:
                    item = self.outbox.claim()
                except Exception as exc:
                    logger.warning("Xinchao outbox claim failed: %s", safe_error_detail(exc))
            if item is None:
                event = self._worker_event
                if event is None:
                    return
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=poll_seconds)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self.deliver(item)
            except Exception as exc:
                logger.warning("Xinchao delivery task failed: %s", safe_error_detail(exc))
                if self.outbox:
                    self.outbox.mark_retry(item, "unexpected")

    def status(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        outbox_status = self.outbox.status() if self.outbox else {"enabled": False}
        return {
            "enabled": True,
            "configured": self.configured,
            "reachable": self._reachable,
            "auth_degraded": self._auth_degraded,
            "last_error_code": self._last_error_code,
            "last_context_success_at": self._last_context_success_at,
            "last_delivery_success_at": self._last_delivery_success_at,
            "outbox": outbox_status,
        }

    async def aclose(self) -> None:
        if self._owns_http_client and self.http_client:
            await self.http_client.aclose()
