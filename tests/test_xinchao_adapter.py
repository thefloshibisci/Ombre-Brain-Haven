import hashlib
import json
import os

import httpx
import pytest

from xinchao_adapter import XinchaoAdapter, XinchaoContext


def adapter_config(**overrides):
    config = {
        "enabled": True,
        "base_url": "http://127.0.0.1:8787",
        "service_token_env": "XINCHAO_SERVICE_TOKEN",
        "context_max_tokens": 600,
        "read_timeout_seconds": 1.5,
        "write_timeout_seconds": 2.0,
        "continuity_limit": 6,
        "notify_dynamic_state": True,
        "outbox_enabled": True,
        "outbox_max_attempts": 8,
        "outbox_max_age_hours": 72,
        "outbox_poll_seconds": 5,
        "session_id_max_length": 160,
    }
    config.update(overrides)
    return {"xinchao_adapter": config}


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = list(responses or [])

    async def get(self, url, *, params=None, headers=None, timeout=None):
        self.requests.append(("GET", url, params, headers, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(*item)

    async def post(self, url, *, json=None, headers=None, timeout=None):
        self.requests.append(("POST", url, json, headers, timeout))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(*item)


def make_adapter(tmp_path, http_client=None, **overrides):
    os.environ["XINCHAO_SERVICE_TOKEN"] = "test-token"
    return XinchaoAdapter(
        adapter_config(**overrides),
        db_path=str(tmp_path / "gateway_state.db"),
        http_client=http_client,
    )


def test_disabled_adapter_is_inert(tmp_path):
    adapter = make_adapter(tmp_path, enabled=False)

    assert adapter.enabled is False
    assert adapter.base_url == ""
    assert adapter.outbox is None


def test_session_mapping_is_deterministic_and_compatible(tmp_path):
    adapter = make_adapter(tmp_path)

    mapped = adapter.map_session_id(" Desktop ", " My::Session 01 ")

    assert mapped == "gateway:Desktop:My::Session-01"
    assert adapter.map_session_id(" Desktop ", "My::Session 01") == mapped


def test_session_mapping_rejects_control_characters(tmp_path):
    adapter = make_adapter(tmp_path)

    with pytest.raises(ValueError):
        adapter.map_session_id("desktop", "bad\x1fsession")


def test_session_mapping_hashes_oversized_ids(tmp_path):
    adapter = make_adapter(tmp_path)
    long_session = "s" * 300

    mapped = adapter.map_session_id("desktop", long_session)

    normalized = f"gateway:desktop:{long_session}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    assert mapped == f"{normalized[:135]}:{digest}"


def test_stable_event_id_is_deterministic(tmp_path):
    adapter = make_adapter(tmp_path)

    first = adapter.build_stable_event_id(
        "profile", "gateway:desktop:main", 3, "user", "openai", "hello"
    )
    second = adapter.build_stable_event_id(
        "profile", "gateway:desktop:main", 3, "user", "openai", "hello"
    )
    different = adapter.build_stable_event_id(
        "profile", "gateway:desktop:main", 3, "user", "openai", "different"
    )

    assert first == second
    assert first.startswith("ombre-gw-v1:")
    assert first != different


async def test_fetch_context_uses_turn_mode_and_token_budget(tmp_path):
    client = FakeClient(
        [
            (
                200,
                {
                    "delivered": True,
                    "sections": [
                        {"id": "dynamic_state", "content": "tone is focused"},
                        {"id": "cross_client_recent", "content": "recent line"},
                        {"id": "dream_residue", "content": "must be ignored"},
                    ],
                    "additionalContext": "ignored",
                    "estimatedTokens": 99,
                },
            )
        ]
    )
    adapter = make_adapter(tmp_path, http_client=client)

    result = await adapter.fetch_context("gateway:desktop:main")

    assert isinstance(result, XinchaoContext)
    assert result.section_count == 2
    assert "tone is focused" in result.text
    assert "recent line" in result.text
    assert "must be ignored" not in result.text
    method, url, params, headers, timeout = client.requests[0]
    assert method == "GET"
    assert url == "http://127.0.0.1:8787/v1/context"
    assert params["mode"] == "turn"
    assert params["max_tokens"] == 600
    assert timeout == 1.5
    assert headers["Authorization"] == "Bearer test-token"


async def test_fetch_context_dedupes_by_normalized_text(tmp_path):
    client = FakeClient(
        [
            (
                200,
                {
                    "delivered": True,
                    "sections": [
                        {"id": "cross_client_recent", "content": "same text"},
                        {"id": "cross_client_recent", "content": "new text"},
                    ],
                },
            )
        ]
    )
    adapter = make_adapter(tmp_path, http_client=client)

    result = await adapter.fetch_context(
        "gateway:desktop:main",
        existing_texts=["same text"],
    )

    assert result.section_count == 1
    assert "same text" not in result.text
    assert "new text" in result.text


async def test_fetch_context_ignores_empty_and_malformed_payloads(tmp_path):
    empty_client = FakeClient([(200, {"delivered": False, "sections": []})])
    malformed_client = FakeClient([(200, {"sections": "not-a-list"})])
    empty_adapter = make_adapter(tmp_path / "a", http_client=empty_client)
    malformed_adapter = make_adapter(tmp_path / "b", http_client=malformed_client)

    empty = await empty_adapter.fetch_context("gateway:desktop:main")
    malformed = await malformed_adapter.fetch_context("gateway:desktop:main")

    assert empty.text == ""
    assert empty.section_count == 0
    assert malformed.text == ""
    assert malformed.degraded is True


async def test_fetch_context_degrades_on_timeout_without_retry(tmp_path):
    client = FakeClient([httpx.ReadTimeout("timed out")])
    adapter = make_adapter(tmp_path, http_client=client)

    result = await adapter.fetch_context("gateway:desktop:main")

    assert result.text == ""
    assert result.degraded is True
    assert len(client.requests) == 1


def test_continuity_outbox_payload_is_bounded_and_stable(tmp_path):
    adapter = make_adapter(tmp_path)

    item = adapter.build_continuity_outbox_item(
        profile_id="profile",
        gateway_session_id="gateway:desktop:main",
        round_id=7,
        route="openai",
        user_text="hello",
        assistant_text="hi",
    )

    assert item["kind"] == "continuity"
    assert item["payload"]["session_id"] == "gateway:desktop:main"
    assert item["payload"]["client"] == "desktop"
    assert item["payload"]["limit"] == 6
    assert len(item["payload"]["messages"]) == 2
    assert item["payload"]["messages"][0]["role"] == "user"
    assert item["payload"]["messages"][1]["role"] == "assistant"
    assert item["payload"]["messages"][0]["text"] == "hello"
    assert item["event_key"] == adapter.build_stable_event_id(
        "profile",
        "gateway:desktop:main",
        7,
        "continuity_round",
        "openai",
        "hello\0hi",
    )


def test_dynamic_event_outbox_payload_uses_whitelisted_tone(tmp_path):
    adapter = make_adapter(tmp_path)

    item = adapter.build_dynamic_event_outbox_item(
        profile_id="profile",
        gateway_session_id="gateway:desktop:main",
        round_id=7,
        route="openai",
        tone="focused",
    )

    assert item["kind"] == "conversation_event"
    assert item["payload"] == {
        "event_id": adapter.build_stable_event_id(
            "profile", "gateway:desktop:main", 7, "round", "openai", ""
        ),
        "session_id": "gateway:desktop:main",
        "tone": "focused",
    }


async def test_outbox_delivery_marks_delivered(tmp_path):
    client = FakeClient([(200, {"accepted": 2, "duplicates": 0})])
    adapter = make_adapter(tmp_path, http_client=client)
    item = adapter.build_continuity_outbox_item(
        profile_id="profile",
        gateway_session_id="gateway:desktop:main",
        round_id=1,
        route="openai",
        user_text="hello",
        assistant_text="hi",
    )

    adapter.outbox.enqueue(item)
    claimed = adapter.outbox.claim()
    await adapter.deliver(claimed)

    assert adapter.outbox.pending_count() == 0
    assert client.requests[0][0] == "POST"
    assert client.requests[0][1] == "http://127.0.0.1:8787/v1/continuity/sync"


async def test_outbox_retries_server_errors_and_fails_auth(tmp_path):
    retry_client = FakeClient([(500, {"error": "server"})])
    retry_adapter = make_adapter(tmp_path / "retry", http_client=retry_client)
    retry_item = retry_adapter.build_continuity_outbox_item(
        profile_id="profile",
        gateway_session_id="gateway:desktop:main",
        round_id=1,
        route="openai",
        user_text="hello",
        assistant_text="hi",
    )
    retry_adapter.outbox.enqueue(retry_item)
    claimed = retry_adapter.outbox.claim()
    await retry_adapter.deliver(claimed)

    assert retry_adapter.outbox.pending_count() == 1

    auth_client = FakeClient([(401, {"error": "unauthorized"})])
    auth_adapter = make_adapter(tmp_path / "auth", http_client=auth_client)
    auth_item = auth_adapter.build_continuity_outbox_item(
        profile_id="profile",
        gateway_session_id="gateway:desktop:main",
        round_id=1,
        route="openai",
        user_text="hello",
        assistant_text="hi",
    )
    auth_adapter.outbox.enqueue(auth_item)
    auth_claimed = auth_adapter.outbox.claim()
    await auth_adapter.deliver(auth_claimed)

    assert auth_adapter.outbox.failed_count() == 1
