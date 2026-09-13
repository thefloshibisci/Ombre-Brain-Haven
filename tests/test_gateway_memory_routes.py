import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import gateway
from raw_events import RawEventStore
from web import gateway_memory
from web import _shared as sh
from starlette.requests import Request


class _RawStore:
    def search(self, query, **kwargs):
        return {"ok": True, "query": query, "count": 0, "items": []}


class _State:
    def list_injection_debug(self, **kwargs):
        return []


class _Service:
    gateway_token = "gateway-test-token"
    raw_event_store = _RawStore()
    state_store = _State()
    care_scheduler = None

    handle_raw_search = gateway.GatewayService.handle_raw_search
    handle_daily_chat_memory_confirm = gateway.GatewayService.handle_daily_chat_memory_confirm
    handle_daily_chat_memory_run = gateway.GatewayService.handle_daily_chat_memory_run
    handle_daily_chat_memory_pending = gateway.GatewayService.handle_daily_chat_memory_pending
    handle_memory_trace = gateway.GatewayService.handle_memory_trace

    def _authorize(self, value):
        if value != "Bearer gateway-test-token":
            return gateway.JSONResponse({"error": "unauthorized"}, status_code=401)
        return None


def _app(service=None):
    service = service or _Service()
    app = gateway.create_gateway_app(service=service, config={"gateway": {}})
    app.state.gateway_service = service
    return app


@pytest.mark.asyncio
async def test_raw_route_requires_gateway_authentication():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/api/search-raw?q=hello")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_raw_route_uses_gateway_store_and_bounds_query():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(
            "/api/search-raw?q=hello&limit=999",
            headers={"Authorization": "Bearer gateway-test-token"},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "query": "hello", "count": 0, "items": []}


@pytest.mark.asyncio
async def test_candidate_routes_are_not_available_without_scheduler():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/api/daily-chat-memory/confirm",
            json={"candidate_ids": ["candidate-1"], "action": "confirm"},
            headers={"Authorization": "Bearer gateway-test-token"},
        )
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


@pytest.mark.asyncio
async def test_cookie_boundary_forwards_authenticated_request_to_fixed_gateway(monkeypatch, tmp_path):
    store = RawEventStore({"state_dir": str(tmp_path)})
    store.ingest([{"role":"user", "text":"only this session", "session_id":"A", "created_at":"2026-09-13T23:59:59+08:00"},
                  {"role":"user", "text":"another session", "session_id":"B", "created_at":"2026-09-13T23:59:59+08:00"}], source="gateway")
    service = _Service()
    service.raw_event_store = store
    transport = httpx.ASGITransport(app=_app(service))
    original_client = httpx.AsyncClient
    calls = []
    def client(**kwargs):
        calls.append(kwargs)
        return original_client(transport=transport, **kwargs)
    monkeypatch.setattr(gateway_memory.httpx, "AsyncClient", client)
    monkeypatch.setattr(sh, "_sessions", {"valid-cookie": time.time() + 60})
    monkeypatch.setenv("OMBRE_GATEWAY_ADMIN_URL", "http://gateway.test/api/config")
    monkeypatch.setenv("OMBRE_GATEWAY_TOKEN", "gateway-test-token")
    scope = {"type":"http", "method":"GET", "path":"/api/search-raw", "headers":[],
             "query_string":b"session_id=A&since=2026-09-13&until=2026-09-13&url=https://invalid.test", "server":("test",80), "scheme":"http"}
    assert (await gateway_memory.forward(Request(scope))).status_code == 401
    assert not calls
    scope["headers"] = [(b"cookie", b"ombre_session=valid-cookie")]
    response = await gateway_memory.forward(Request(scope))
    import json
    body = json.loads(response.body)
    assert response.status_code == 200
    assert [row["text"] for row in body["items"]] == ["only this session"]
    assert b"gateway-test-token" not in response.body and b"metadata" not in response.body
    assert calls[0]["follow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("params", ["role=system", "since=invalid", "since=2026-09-14&until=2026-09-13", "event_ids=1,secret"])
async def test_invalid_archive_filters_fail_without_echoing_values(params):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get('/api/search-raw?' + params, headers={"Authorization":"Bearer gateway-test-token"})
    assert response.status_code == 400
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_candidate_decision_validation_and_failures():
    service = _Service()
    confirm = AsyncMock(return_value={"status":"ok", "created":1})
    service.care_scheduler = SimpleNamespace(confirm_daily_chat_memory=confirm)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(service)), base_url="http://test", headers={"Authorization":"Bearer gateway-test-token"}) as client:
        for body in ({"candidate_ids":[], "action":"confirm"}, {"ids":[True]}, {"ids":["x"], "action":"delete"}, {"ids":["x"], "edits":{"x":"bad"}}):
            assert (await client.post('/api/daily-chat-memory/confirm', json=body)).status_code == 400
        confirm.assert_not_awaited()
        response = await client.post('/api/daily-chat-memory/confirm', json={"ids":["x"], "action":"reject"})
        assert response.status_code == 200
        confirm.assert_awaited_once_with(["x"], action="reject", edits={})
        confirm.side_effect = RuntimeError("private-path private-key")
        failure = await client.post('/api/daily-chat-memory/confirm', json={"ids":["x"]})
        assert failure.status_code == 503
        assert "private" not in failure.text


@pytest.mark.asyncio
async def test_trace_projection_excludes_unrequested_internal_content():
    service = _Service()
    service.state_store = SimpleNamespace(list_injection_debug=lambda **kw:[{
        "id":1,"session_id":"A","round_id":2,"created_at":"2026-09-13",
        "payload":{"injected_bucket_ids":["memory-1"],"recalled_bucket_ids":["memory-1","memory-2"],"system_prompt":"private", "dynamic_context":"private"}}])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(service)), base_url="http://test", headers={"Authorization":"Bearer gateway-test-token"}) as client:
        response = await client.get('/api/gateway-injections')
    assert response.status_code == 200
    assert response.json()["items"][0]["payload"]["injected_bucket_ids"] == ["memory-1"]
    assert "private" not in response.text
