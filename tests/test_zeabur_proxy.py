import json

import httpx
import pytest

import entrypoint_zeabur
import proxy_server


@pytest.mark.asyncio
async def test_proxy_routes_and_preserves_gateway_prefix(monkeypatch):
    requests = []

    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.headers = httpx.Headers({"content-type": "application/json"})

        async def aread(self):
            return b'{"ok":true}'

        async def aclose(self):
            return None

    class FakeAsyncClient:
        def __init__(self, base_url, timeout=None):
            self.base_url = base_url
            self.timeout = timeout

        def build_request(self, method, url, headers=None, content=None):
            requests.append((str(self.base_url), method, url.path, content))
            return object()

        async def send(self, request, stream=True):
            return FakeResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr(proxy_server, "httpx", type("httpx", (), {"AsyncClient": FakeAsyncClient, "URL": httpx.URL, "InvalidURL": httpx.InvalidURL, "Headers": httpx.Headers}))
    monkeypatch.setenv("OMBRE_BRAIN_URL", "http://brain.internal")
    monkeypatch.setenv("OMBRE_GATEWAY_URL", "http://gateway.internal")
    monkeypatch.setenv("OMBRE_XINCHAO_URL", "http://xinchao.internal")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_server.app),
        base_url="http://proxy.test",
    ) as client:
        brain_response = await client.get("/health")
        gateway_response = await client.post(
            "/v1/chat/completions",
            json={"model": "test"},
            headers={"Authorization": "Bearer token"},
        )
        xinchao_response = await client.get("/xinchao/health")

    assert brain_response.status_code == 200
    assert brain_response.json() == {"ok": True}
    assert gateway_response.status_code == 200
    assert gateway_response.json() == {"ok": True}
    assert xinchao_response.status_code == 200
    assert xinchao_response.json() == {"ok": True}

    assert requests[0][0] == "http://brain.internal"
    assert requests[0][2] == "/health"
    assert requests[1][0] == "http://gateway.internal"
    assert requests[1][2] == "/v1/chat/completions"
    assert json.loads(requests[1][3]) == {"model": "test"}
    assert requests[2][0] == "http://xinchao.internal"
    assert requests[2][2] == "/health"


@pytest.mark.asyncio
async def test_proxy_maps_public_gateway_health_alias(monkeypatch):
    requests = []

    class FakeAsyncClient:
        def __init__(self, base_url, timeout=None):
            pass

        def build_request(self, method, url, headers=None, content=None):
            requests.append(url.path)
            return object()

        async def send(self, request, stream=True):
            return FakeResponse()

        async def aclose(self):
            return None

    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.headers = httpx.Headers({"content-type": "application/json"})

        async def aread(self):
            return b'{"status":"ok"}'

        async def aclose(self):
            return None

    monkeypatch.setattr(proxy_server, "httpx", type("httpx", (), {"AsyncClient": FakeAsyncClient, "URL": httpx.URL, "InvalidURL": httpx.InvalidURL, "Headers": httpx.Headers}))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy_server.app),
        base_url="http://proxy.test",
    ) as client:
        response = await client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert requests == ["/health"]


@pytest.mark.asyncio
async def test_proxy_streams_event_responses(monkeypatch):
    class FakeAsyncClient:
        def __init__(self, base_url, timeout=None):
            self.closed = False

        def build_request(self, method, url, headers=None, content=None):
            return object()

        async def send(self, request, stream=True):
            return FakeStreamResponse(self)

        async def aclose(self):
            self.closed = True

    class FakeStreamResponse:
        def __init__(self, client):
            self.client = client
            self.status_code = 200
            self.headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_raw(self):
            yield b"data: ok\n\n"

        async def aclose(self):
            return None

    fake_client = FakeAsyncClient("http://xinchao.internal")

    class FakeClientFactory(FakeAsyncClient):
        pass

    class FakeProxyHTTPX:
        AsyncClient = lambda base_url, timeout=None: fake_client
        URL = httpx.URL
        InvalidURL = httpx.InvalidURL
        Headers = httpx.Headers

    monkeypatch.setattr(proxy_server, "httpx", FakeProxyHTTPX)
    monkeypatch.setenv("OMBRE_XINCHAO_URL", "http://xinchao.internal")

    transport = httpx.ASGITransport(app=proxy_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy.test") as client:
        response = await client.get("/xinchao/events")

    assert response.status_code == 200
    assert response.text == "data: ok\n\n"
    assert fake_client.closed is True


def test_entrypoint_creates_credential_free_runtime_config(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.setenv("OMBRE_STATE_DIR", str(state_dir))
    monkeypatch.setenv("OMBRE_BUCKETS_DIR", str(data_dir))

    entrypoint_zeabur.ensure_runtime_config()

    runtime = entrypoint_zeabur.yaml.safe_load(
        (state_dir / "config.runtime.yaml").read_text(encoding="utf-8")
    )
    adapter = runtime["gateway"]["xinchao_adapter"]
    assert adapter["enabled"] is True
    assert adapter["service_token_env"] == "SERVICE_TOKEN"
    assert runtime["raw_events"]["db_path"] == "/data/raw_events.sqlite"


def test_entrypoint_isolates_child_ports(monkeypatch):
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.setenv("OMBRE_XINCHAO_PORT", "18110")
    monkeypatch.setenv("OMBRE_PROXY_PORT", "9123")
    monkeypatch.delenv("OMBRE_PORT", raising=False)
    monkeypatch.delenv("OMBRE_GATEWAY_PORT", raising=False)

    proxy_env = entrypoint_zeabur.build_child_env("proxy")
    brain_env = entrypoint_zeabur.build_child_env("brain")
    gateway_env = entrypoint_zeabur.build_child_env("gateway")
    xinchao_env = entrypoint_zeabur.build_child_env("xinchao")

    assert proxy_env["OMBRE_PROXY_PORT"] == "9123"
    assert "PORT" not in proxy_env
    assert brain_env["OMBRE_PORT"] == "8000"
    assert brain_env["PORT"] == "8000"
    assert gateway_env["OMBRE_GATEWAY_PORT"] == "8010"
    assert "PORT" not in gateway_env
    assert xinchao_env["PORT"] == "18110"


def test_entrypoint_prefers_explicit_proxy_port(monkeypatch):
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("OMBRE_PROXY_PORT", "9000")

    proxy_env = entrypoint_zeabur.build_child_env("proxy")

    assert proxy_env["OMBRE_PROXY_PORT"] == "9000"
    assert "PORT" not in proxy_env
