import asyncio
import base64
from pathlib import Path

import httpx
import pytest

from bucket_manager import BucketManager
from media_store import MediaPersistenceError, MediaStore


PNG_BYTES = b"\x89PNG\r\n\x1a\nnot-a-real-image-but-stable-test-bytes"


def run(awaitable):
    return asyncio.run(awaitable)


def public_dns(monkeypatch):
    monkeypatch.setattr(MediaStore, "_host_is_public", staticmethod(lambda _host: True))


def test_base64_and_local_file_are_persisted_and_resolved(tmp_path):
    store = MediaStore(str(tmp_path / "vault"))
    local_file = tmp_path / "moment.jpg"
    local_file.write_bytes(b"local-photo")

    items = run(
        store.persist(
            "memory-1",
            [
                {
                    "data_base64": base64.b64encode(PNG_BYTES).decode("ascii"),
                    "filename": "moment.png",
                    "title": "雨后",
                    "note": "我想起了那天。",
                },
                {"path": str(local_file)},
            ],
        )
    )

    assert len(items) == 2
    assert items[0]["type"] == "image/png"
    assert items[0]["title"] == "雨后"
    assert items[0]["note"] == "我想起了那天。"
    assert store.resolve("memory-1", items[0]).read_bytes() == PNG_BYTES
    assert store.resolve("memory-1", items[1]).read_bytes() == b"local-photo"
    assert store.resolve("another-memory", items[0]) is None


def test_duplicate_media_in_one_request_is_stored_once(tmp_path):
    store = MediaStore(str(tmp_path))
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    items = run(
        store.persist(
            "memory-duplicate",
            [
                {"data_base64": encoded, "filename": "first.png", "title": "第一次"},
                {"data_base64": encoded, "filename": "second.png", "title": "第二次"},
            ],
        )
    )
    assert len(items) == 1
    assert items[0]["title"] == "第二次"
    assert len(list((tmp_path / "_media" / "memory-duplicate").iterdir())) == 1


def test_url_download_follows_public_redirect_and_keeps_final_url(tmp_path, monkeypatch):
    public_dns(monkeypatch)

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/photo.png"})
        return httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})

    store = MediaStore(str(tmp_path), transport=httpx.MockTransport(handler))
    item = run(store.persist("memory-2", "https://images.example/start"))[0]

    assert item["source_url"] == "https://images.example/photo.png"
    assert store.resolve("memory-2", item).read_bytes() == PNG_BYTES


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/photo.png",
        "http://127.0.0.1/photo.png",
        "http://10.0.0.2/photo.png",
        "http://user:secret@example.com/photo.png",
        "file:///tmp/photo.png",
    ],
)
def test_private_or_credentialed_urls_are_rejected(tmp_path, url):
    store = MediaStore(str(tmp_path), transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    with pytest.raises(MediaPersistenceError):
        run(store.persist("memory-3", url))


def test_bad_content_length_http_failure_and_size_limit(tmp_path, monkeypatch):
    public_dns(monkeypatch)

    cases = [
        (httpx.Response(200, content=b"x", headers={"content-length": "banana"}), "Content-Length"),
        (httpx.Response(200, content=b"12345", headers={"content-length": "5"}), "超过"),
        (httpx.Response(404, content=b"missing"), "HTTP 404"),
    ]
    for response, message in cases:
        store = MediaStore(
            str(tmp_path / message.replace(" ", "-")),
            max_bytes=4,
            transport=httpx.MockTransport(lambda _request, response=response: response),
        )
        with pytest.raises(MediaPersistenceError, match=message):
            run(store.persist("memory-4", "https://images.example/photo.png"))


def test_failed_batch_rolls_back_files_created_earlier_in_batch(tmp_path):
    store = MediaStore(str(tmp_path))
    with pytest.raises(MediaPersistenceError):
        run(
            store.persist(
                "memory-5",
                [
                    {"data_base64": base64.b64encode(PNG_BYTES).decode("ascii"), "filename": "ok.png"},
                    {"data_base64": "definitely-not-base64", "filename": "bad.png"},
                ],
            )
        )

    media_dir = tmp_path / "_media" / "memory-5"
    assert not media_dir.exists() or not list(media_dir.iterdir())


def test_bucket_manager_media_replace_append_clear_and_deduplicate(tmp_path):
    manager = BucketManager({"buckets_dir": str(tmp_path)})
    first = {"data_base64": base64.b64encode(b"first").decode("ascii"), "filename": "first.jpg"}
    second = {"data_base64": base64.b64encode(b"second").decode("ascii"), "filename": "second.jpg"}

    bucket_id = run(manager.create(content="有一张照片。", media=first))
    bucket = run(manager.get(bucket_id))
    assert len(bucket["metadata"]["media"]) == 1

    assert run(manager.update(bucket_id, media_append=[first, second]))
    bucket = run(manager.get(bucket_id))
    assert len(bucket["metadata"]["media"]) == 2
    assert len({item["sha256"] for item in bucket["metadata"]["media"]}) == 2

    assert run(manager.update(bucket_id, media=[second]))
    bucket = run(manager.get(bucket_id))
    assert len(bucket["metadata"]["media"]) == 1
    assert manager.media_store.resolve(bucket_id, bucket["metadata"]["media"][0]).read_bytes() == b"second"
    assert len(list((tmp_path / "_media" / bucket_id).iterdir())) == 1

    assert run(manager.update(bucket_id, media=[]))
    bucket = run(manager.get(bucket_id))
    assert "media" not in bucket["metadata"]
    assert not (tmp_path / "_media" / bucket_id).exists()
