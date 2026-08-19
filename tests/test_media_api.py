import asyncio
import base64
from types import SimpleNamespace

from bucket_manager import BucketManager
import server


def run(awaitable):
    return asyncio.run(awaitable)


def request(bucket_id, media_index, token=""):
    return SimpleNamespace(
        cookies={"ombre_session": token} if token else {},
        path_params={"bucket_id": bucket_id, "media_index": str(media_index)},
    )


def test_bucket_media_route_requires_auth_and_serves_only_bucket_media(tmp_path, monkeypatch):
    manager = BucketManager({"buckets_dir": str(tmp_path)})
    bucket_id = run(
        manager.create(
            content="留下这一张画面。",
            media={
                "data_base64": base64.b64encode(b"test-image").decode("ascii"),
                "filename": "moment.png",
            },
        )
    )
    monkeypatch.setattr(server, "bucket_mgr", manager)

    unauthorized = run(server.api_bucket_media(request(bucket_id, 0)))
    assert unauthorized.status_code == 401

    token = server._create_dashboard_session()
    response = run(server.api_bucket_media(request(bucket_id, 0, token)))
    assert response.status_code == 200
    assert response.media_type == "image/png"
    assert response.headers["cache-control"] == "private, max-age=3600"
    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert manager.media_store.resolve(
        bucket_id,
        (run(manager.get(bucket_id))["metadata"]["media"])[0],
    ).samefile(response.path)

    missing = run(server.api_bucket_media(request(bucket_id, 99, token)))
    assert missing.status_code == 404


def test_bucket_media_route_rejects_invalid_identifiers(tmp_path, monkeypatch):
    manager = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(server, "bucket_mgr", manager)
    token = server._create_dashboard_session()

    invalid_id = run(server.api_bucket_media(request("../escape", 0, token)))
    assert invalid_id.status_code == 400

    invalid_index_request = request("safe-id", 0, token)
    invalid_index_request.path_params["media_index"] = "not-a-number"
    invalid_index = run(server.api_bucket_media(invalid_index_request))
    assert invalid_index.status_code == 400
