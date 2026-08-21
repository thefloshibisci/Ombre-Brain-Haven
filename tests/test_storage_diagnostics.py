import asyncio
import json
import sqlite3
from types import SimpleNamespace

import server
from bucket_manager import BucketManager


def run(awaitable):
    return asyncio.run(awaitable)


def request(token=""):
    return SimpleNamespace(cookies={"ombre_session": token} if token else {}, path_params={})


def test_embedding_storage_diagnostics_reports_valid_and_orphan_rows(tmp_path):
    db_path = tmp_path / "embeddings.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE embeddings (bucket_id TEXT PRIMARY KEY, embedding TEXT NOT NULL, model TEXT, dimension INTEGER)"
        )
        conn.executemany(
            "INSERT INTO embeddings VALUES (?, ?, ?, ?)",
            [
                ("live", json.dumps([0.1, 0.2]), "test-model", 2),
                ("old-model", json.dumps([0.2, 0.3]), "old-model", 2),
                ("orphan", json.dumps([0.3, 0.4]), "test-model", 2),
                ("bad-dimension", json.dumps([0.5]), "test-model", 2),
            ],
        )

    report = server._read_embedding_storage_diagnostics(
        str(db_path),
        {"live", "old-model", "bad-dimension", "missing"},
        "test-model",
    )

    assert report["exists"] is True
    assert report["quick_check"] == "ok"
    assert report["row_count"] == 4
    assert report["bucket_rows"] == 3
    assert report["compatible_current_rows"] == 1
    assert report["missing_bucket_rows"] == 1
    assert report["orphan_rows"] == 1
    assert report["invalid_vector_rows"] == 1
    assert report["incompatible_model_rows"] == 1
    assert report["models"] == {"test-model": 3, "old-model": 1}
    assert report["dimensions"] == {"2": 4}


def test_storage_diagnostics_requires_dashboard_session_and_reports_media(tmp_path, monkeypatch):
    manager = BucketManager({"buckets_dir": str(tmp_path / "buckets")})
    bucket_id = run(
        manager.create(
            content="带图的记忆",
            media={"data_base64": "aGVsbG8=", "filename": "moment.txt"},
        )
    )
    monkeypatch.setattr(server, "bucket_mgr", manager)
    monkeypatch.setattr(server, "config", {"buckets_dir": str(tmp_path / "buckets")})
    monkeypatch.setattr(server.embedding_engine, "db_path", str(tmp_path / "buckets" / "embeddings.db"))

    unauthorized = run(server.api_storage_diagnostics(request()))
    assert unauthorized.status_code == 401

    token = server._create_dashboard_session()
    response = run(server.api_storage_diagnostics(request(token)))
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["storage"]["bucket_records"] == 1
    assert payload["media"]["references"] == 1
    assert payload["media"]["readable"] == 1
    assert payload["media"]["missing"] == 0
    assert payload["embeddings"]["exists"] is False
