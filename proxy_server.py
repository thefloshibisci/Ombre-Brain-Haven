"""Minimal reverse proxy for Zeabur's single public port.

Haven MCP/Dashboard remains the default route. The gateway and Xinchao are
internal sidecars; only their public prefixes are exposed here.
"""

import os
import logging

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route


logger = logging.getLogger("ombre_proxy")


DEFAULT_TARGETS = {
    "brain": "http://127.0.0.1:8000",
    "gateway": "http://127.0.0.1:8010",
    "xinchao": "http://127.0.0.1:18110",
}


def _target_for_path(path: str) -> tuple[str, str]:
    if path == "/xinchao" or path.startswith("/xinchao/"):
        target = os.environ.get("OMBRE_XINCHAO_URL", DEFAULT_TARGETS["xinchao"])
        return target, path[len("/xinchao"):] or "/"
    if path == "/v1" or path.startswith("/v1/"):
        return os.environ.get("OMBRE_GATEWAY_URL", DEFAULT_TARGETS["gateway"]), path
    return os.environ.get("OMBRE_BRAIN_URL", DEFAULT_TARGETS["brain"]), path


def _proxy_headers(request: Request) -> dict[str, str]:
    headers = []
    for name, value in request.headers.items():
        if name.lower() in {"host", "content-length"}:
            continue
        headers.append((name, value))
    return dict(headers)


async def proxy(request: Request) -> Response:
    target, path = _target_for_path(request.url.path)
    url = httpx.URL(path=path, query=request.url.query.encode("utf-8"))
    headers = _proxy_headers(request)
    request_body = await request.body()

    try:
        client = httpx.AsyncClient(base_url=target, timeout=None)
        upstream_request = client.build_request(
            request.method,
            url,
            headers=headers,
            content=request_body,
        )
    except httpx.InvalidURL:
        return Response("Bad Gateway", status_code=502)

    try:
        upstream = await client.send(upstream_request, stream=True)
    except httpx.RequestError:
        await client.aclose()
        return Response("Bad Gateway", status_code=502)

    response_headers = [
        (name, value)
        for name, value in upstream.headers.items()
        if name.lower() not in {"connection", "transfer-encoding", "keep-alive"}
    ]

    if "text/event-stream" in upstream.headers.get("content-type", "").lower():
        async def close_stream():
            await upstream.aclose()
            await client.aclose()

        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=dict(response_headers),
            background=BackgroundTask(close_stream),
        )

    content = await upstream.aread()
    await upstream.aclose()
    await client.aclose()
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=dict(response_headers),
    )


async def healthz(request: Request) -> Response:
    return Response("ok", media_type="text/plain")


routes = [
    Route("/healthz", healthz, methods=["GET"]),
    Route("/{path:path}", proxy, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
]

app = Starlette(routes=routes)


def main() -> None:
    port = int(os.environ.get("OMBRE_PROXY_PORT", "9000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
