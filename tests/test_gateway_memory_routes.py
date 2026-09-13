import json

import httpx
import pytest

import gateway


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

    def _authorize(self, value):
        if value != "Bearer gateway-test-token":
            return gateway.JSONResponse({"error": "unauthorized"}, status_code=401)
        return None


def _app():
    service = _Service()
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
