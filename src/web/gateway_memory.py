"""Dashboard session boundary for Gateway-owned archive and review operations."""
import os
from urllib.parse import urlsplit, urlunsplit

import httpx
from starlette.responses import JSONResponse

from . import _shared as sh


ROUTES = {
    "/api/search-raw": ("GET",),
    "/api/gateway-injections": ("GET",),
    "/api/recall-debug": ("GET",),
    "/api/daily-chat-memory/run": ("GET", "POST"),
    "/api/daily-chat-memory/confirm": ("POST",),
}


def configured():
    return bool(os.environ.get("OMBRE_GATEWAY_ADMIN_URL") and os.environ.get("OMBRE_GATEWAY_TOKEN"))


async def forward(request, path=None):
    if (error := sh._require_auth(request)) is not None:
        return error
    path = path or request.url.path
    if path not in ROUTES and path != "/api/daily-chat-memory/pending":
        return JSONResponse({"error": "Unknown memory operation"}, status_code=404)
    parts = urlsplit(os.environ.get("OMBRE_GATEWAY_ADMIN_URL", ""))
    if not configured() or parts.scheme not in {"http", "https"} or not parts.netloc:
        return JSONResponse({"error": "Gateway memory service is unavailable"}, status_code=503)
    body = None
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Expected an object"}, status_code=400)
    url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            response = await client.request(
                request.method, url, params=request.query_params,
                headers={"Authorization": "Bearer " + os.environ["OMBRE_GATEWAY_TOKEN"]},
                **({"json": body} if body is not None else {}),
            )
        if response.status_code not in {200, 202, 400, 409}:
            raise ValueError("gateway request failed")
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("invalid gateway response")
        return JSONResponse(data, status_code=response.status_code, headers={"Cache-Control": "no-store"})
    except Exception as exc:
        sh.logger.warning("Gateway memory request failed: %s", type(exc).__name__)
        return JSONResponse({"error": "Gateway memory service is unavailable"}, status_code=503)


def register(mcp):
    for path, methods in ROUTES.items():
        mcp.custom_route(path, methods=list(methods))(forward)
