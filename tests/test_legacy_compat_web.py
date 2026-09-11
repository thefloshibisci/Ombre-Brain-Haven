from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

from web import legacy_compat as compat
from web import _shared as sh


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(fn):
            self.routes[(path, tuple(methods))] = fn
            return fn
        return decorator


def request(path: str = "/", *, method: str = "GET", body: bytes = b"", path_params: dict | None = None) -> Request:
    raw_path, _, query = path.partition("?")
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http", "method": method, "path": raw_path,
        "raw_path": raw_path.encode(), "query_string": query.encode(),
        "headers": [], "client": ("127.0.0.1", 1), "scheme": "http",
        "server": ("test", 80), "path_params": path_params or {},
    }
    return Request(scope, receive)


def json_request(path: str, payload: object, *, method: str = "POST", path_params: dict | None = None) -> Request:
    return request(path, method=method, body=json.dumps(payload).encode("utf-8"), path_params=path_params)


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    for name in (
        "OMBRE_REMINDER_DB_PATH", "OMBRE_DAILY_CHAT_MEMORY_PENDING_PATH",
        "OMBRE_PERSONA_DB_PATH", "OMBRE_PORTRAIT_STATE_PATH",
        "OMBRE_DREAM_DATA_DIR", "OMBRE_DREAMS_DIR", "OMBRE_WORD_MAP_DB_PATH",
        "OMBRE_GATEWAY_ADMIN_URL", "OMBRE_GATEWAY_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sh, "repo_root", str(tmp_path))
    monkeypatch.setattr(sh, "config", {
        "state_dir": str(tmp_path / "state"),
        "buckets_dir": str(tmp_path / "buckets"),
        "persona": {"profile_id": "main_profile", "enabled": True},
        "dream": {"enabled": True},
    })
    monkeypatch.setattr(sh, "_require_auth", lambda _request: None)
    (tmp_path / "state").mkdir()
    (tmp_path / "buckets").mkdir()
    monkeypatch.setattr(sh, "bucket_mgr", None)
    return tmp_path


def test_readonly_sqlite_does_not_create_or_write(tmp_path):
    db = tmp_path / "read.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table t (value text)")
    conn.execute("insert into t values ('before')")
    conn.commit()
    conn.close()

    ro = compat._readonly_connect(db)
    assert ro.execute("select value from t").fetchone()[0] == "before"
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("insert into t values ('after')")
    ro.close()
    assert sqlite3.connect(db).execute("select count(*) from t").fetchone()[0] == 1
    with pytest.raises(FileNotFoundError):
        compat._readonly_connect(tmp_path / "missing.sqlite")
    assert not (tmp_path / "missing.sqlite").exists()


def test_registers_compatibility_routes():
    fake = FakeMCP()
    compat.register(fake)
    paths = {path for path, methods in fake.routes}
    assert paths == {
        "/api/reminders", "/api/reminders/{reminder_id}", "/api/moments", "/api/daily-chat-memory/pending",
        "/api/persona", "/api/dreams", "/api/dreams/{dream_id}",
        "/api/portrait-state", "/api/portrait/initialize", "/api/profile-facts", "/api/word-map",
    }
    assert ("/api/reminders", ("GET",)) in fake.routes
    assert ("/api/reminders", ("POST",)) in fake.routes
    assert ("/api/reminders/{reminder_id}", ("PATCH",)) in fake.routes
    assert all(methods in {("GET",), ("POST",), ("PATCH",)} for _path, methods in fake.routes)


@pytest.mark.asyncio
async def test_portrait_initialization_proxy_is_authenticated_and_scoped(isolated_runtime, monkeypatch):
    monkeypatch.setenv("OMBRE_GATEWAY_ADMIN_URL", "http://127.0.0.1:8010/api/config")
    monkeypatch.setenv("OMBRE_GATEWAY_TOKEN", "private-test-token")
    calls = []
    class Client:
        def __init__(self, **options):
            assert options["follow_redirects"] is False
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return httpx.Response(202, json={"status": "running", "state_path": "secret-path", "api_key": "private-test-token", "content": "private"})
    monkeypatch.setattr(compat.httpx, "AsyncClient", Client)
    monkeypatch.setattr(sh, "_require_auth", lambda req: JSONResponse({"error": "Unauthorized"}, status_code=401))
    assert (await compat._portrait_initialization(json_request("/api/portrait/initialize", {}))).status_code == 401
    assert not calls
    monkeypatch.setattr(sh, "_require_auth", lambda req: None)
    assert (await compat._portrait_initialization(json_request("/api/portrait/initialize", {"url": "https://invalid", "force": True}))).status_code == 400
    response = await compat._portrait_initialization(json_request("/api/portrait/initialize", {}))
    assert response.status_code == 202
    assert json.loads(response.body) == {"status": "running"}
    assert calls == [("POST", "http://127.0.0.1:8010/api/portrait/initialize", {
        "headers": {"Authorization": "Bearer private-test-token"}, "json": {},
    })]


@pytest.mark.asyncio
async def test_portrait_proxy_errors_do_not_leak_and_missing_state_can_initialize(isolated_runtime, monkeypatch):
    monkeypatch.setenv("OMBRE_GATEWAY_ADMIN_URL", "http://127.0.0.1:8010/api/config")
    monkeypatch.setenv("OMBRE_GATEWAY_TOKEN", "private-test-token")
    class Client:
        def __init__(self, **options):
            pass
        async def __aenter__(self):
            raise RuntimeError("secret-path private-test-token")
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(compat.httpx, "AsyncClient", Client)
    response = await compat._portrait_initialization(request("/api/portrait/initialize"))
    assert response.status_code == 503
    assert b"secret-path" not in response.body and b"private-test-token" not in response.body
    fake = FakeMCP(); compat.register(fake)
    response = await fake.routes[("/api/portrait-state", ("GET",))](request())
    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["initialized"] is False and data["initialization_available"] is True
    path = isolated_runtime / "state" / "portrait_state.json"
    assert not path.exists()
    path.write_text(json.dumps({"runs": [{"date": "2026-09-11", "raw_response": "hidden"}]}), encoding="utf-8")
    response = await fake.routes[("/api/portrait-state", ("GET",))](request())
    assert json.loads(response.body)["initialized"] is True
    assert b"hidden" not in response.body


@pytest.mark.asyncio
async def test_reminders_pending_and_dream_are_read_only(isolated_runtime):
    tmp_path = isolated_runtime
    db = tmp_path / "state" / "reminders.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table reminders (id text, title text, content text, status text, created_at text, updated_at text, next_due_at text)")
    conn.execute("insert into reminders values ('r1','Care','hydrate','active','2026-08-31','2026-08-31','')")
    conn.execute("insert into reminders values ('r2','Done','x','done','2026-08-30','2026-08-30','')")
    conn.commit(); conn.close()
    pending = tmp_path / "state" / "daily_chat_memory_candidates.json"
    pending.write_text(json.dumps({"items": [
        {"id": "old", "status": "written", "created_at": "2026-08-30"},
        {"id": "new", "status": "pending", "created_at": "2026-08-31"},
    ]}), encoding="utf-8")
    dreams = tmp_path / "state" / "dreams"
    dreams.mkdir()
    (dreams / "dream_1.md").write_text("---\ndream_id: dream_1\ngenerated_at: 2026-08-31T00:00:00\nsurfaced: false\n---\nbody\n", encoding="utf-8")

    fake = FakeMCP(); compat.register(fake)
    reminders = await fake.routes[("/api/reminders", ("GET",))](request("/api/reminders"))
    assert json.loads(reminders.body)["count"] == 1
    pending_response = await fake.routes[("/api/daily-chat-memory/pending", ("GET",))](request("/api/daily-chat-memory/pending"))
    assert json.loads(pending_response.body)["items"][0]["id"] == "new"
    dreams_response = await fake.routes[("/api/dreams", ("GET",))](request("/api/dreams"))
    assert json.loads(dreams_response.body)["records"][0]["dream_id"] == "dream_1"
    detail = await fake.routes[("/api/dreams/{dream_id}", ("GET",))](Request({
        "type": "http", "method": "GET", "path": "/api/dreams/dream_1",
        "raw_path": b"/api/dreams/dream_1", "query_string": b"", "path_params": {"dream_id": "dream_1"},
        "headers": [], "client": ("127.0.0.1", 1), "scheme": "http", "server": ("test", 80),
    }))
    assert json.loads(detail.body)["body"] == "body"
    assert "status_value" in json.loads(detail.body)
    assert "after" not in db.read_bytes().decode("latin1", errors="ignore")


def test_profile_and_moment_projection_accepts_profile_fact_tag():
    profile = {"id": "p1", "content": "likes tea", "metadata": {"tags": ["profile_fact"]}}
    moment = {"id": "reflection_daily_1", "content": "warm", "metadata": {"type": "feel", "tags": ["daily_impression"]}}
    assert compat._profile_fact(profile)["kind"] == "profile_fact"
    assert compat._moment(moment)["content"] == "warm"


@pytest.mark.asyncio
async def test_reminder_create_and_patch_are_scoped_to_reminder_store(isolated_runtime):
    tmp_path = isolated_runtime
    db = tmp_path / "state" / "reminders.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("create table reminders (id text primary key, title text, content text, status text, repeat_rule text, interval_rounds integer, daily_limit integer, max_injections integer, next_due_at text, created_at text, updated_at text, resolved_at text)")
    conn.commit(); conn.close()

    fake = FakeMCP(); compat.register(fake)
    create = fake.routes[("/api/reminders", ("POST",))]
    patch = fake.routes[("/api/reminders/{reminder_id}", ("PATCH",))]

    missing = await create(json_request("/api/reminders", {"title": "only title"}))
    assert missing.status_code == 400
    assert json.loads(missing.body)["read_only"] is False

    created = await create(json_request("/api/reminders", {"title": "喝水", "content": "现在喝一杯水", "repeat_rule": "daily"}))
    assert created.status_code == 201
    created_data = json.loads(created.body)
    assert created_data["read_only"] is False
    reminder_id = created_data["reminder"]["id"]

    updated = await patch(json_request(
        f"/api/reminders/{reminder_id}", {"status": "done"}, method="PATCH",
        path_params={"reminder_id": reminder_id},
    ))
    assert updated.status_code == 200
    updated_data = json.loads(updated.body)
    assert updated_data["read_only"] is False
    assert updated_data["reminder"]["status"] == "done"

    snoozed = await patch(json_request(
        f"/api/reminders/{reminder_id}", {"snooze_minutes": 30}, method="PATCH",
        path_params={"reminder_id": reminder_id},
    ))
    assert snoozed.status_code == 200
    snoozed_data = json.loads(snoozed.body)
    assert snoozed_data["reminder"]["status"] == "active"
    assert snoozed_data["reminder"]["next_due_at"]

    invalid = await patch(json_request(
        "/api/reminders/no/slash", {"status": "done"}, method="PATCH",
        path_params={"reminder_id": "no/slash"},
    ))
    assert invalid.status_code == 400
    assert json.loads(invalid.body)["read_only"] is False

    rows = sqlite3.connect(db).execute("select title, content, status from reminders").fetchall()
    assert rows == [("喝水", "现在喝一杯水", "active")]


def seed_db(path, statements):
    conn = sqlite3.connect(path)
    try:
        with conn:
            for sql, args in statements:
                conn.execute(sql, args)
    finally:
        conn.close()


async def call(path, query="", *, method="GET", body=None, path_params=None):
    fake = FakeMCP()
    compat.register(fake)
    req = json_request(path + query, body, method=method, path_params=path_params)
    return await fake.routes[(path, (method,))](req)


@pytest.mark.asyncio
async def test_all_routes_authenticate_before_opening_storage(isolated_runtime, monkeypatch):
    denial = JSONResponse({"error": "unauthorized"}, status_code=401)
    monkeypatch.setattr(sh, "_require_auth", lambda _: denial)

    def unexpected(*args, **kwargs):
        pytest.fail("unauthenticated request accessed storage")

    monkeypatch.setattr(compat, "_path_for", unexpected)
    monkeypatch.setattr(compat, "_readonly_connect", unexpected)
    fake = FakeMCP()
    compat.register(fake)
    for (path, methods), handler in fake.routes.items():
        response = await handler(request(path, method=methods[0]))
        assert response is denial


@pytest.mark.asyncio
async def test_persona_missing_optional_columns_and_session_filter(isolated_runtime):
    db = isolated_runtime / "state" / "persona_state.db"
    seed_db(db, [
        ("CREATE TABLE persona_global_state (profile_id TEXT, trust REAL, api_key TEXT)", ()),
        ("INSERT INTO persona_global_state VALUES ('main_profile', 0.7, 'hidden-key')", ()),
        ("INSERT INTO persona_global_state VALUES ('other_profile', 0.9, 'hidden-other')", ()),
        ("CREATE TABLE persona_session_state (profile_id TEXT, session_id TEXT, mood_label TEXT, raw_response TEXT)", ()),
        ("INSERT INTO persona_session_state VALUES ('main_profile', 'one', 'calm', 'hidden-raw')", ()),
        ("INSERT INTO persona_session_state VALUES ('main_profile', 'two', 'busy', 'hidden-raw')", ()),
        ("CREATE TABLE persona_events (id INTEGER, profile_id TEXT, session_id TEXT, event_type TEXT, user_excerpt TEXT, raw_response TEXT)", ()),
        ("INSERT INTO persona_events VALUES (1, 'main_profile', 'one', 'warm', 'hidden-excerpt', 'hidden-raw')", ()),
        ("INSERT INTO persona_events VALUES (2, 'main_profile', 'two', 'busy', '', '')", ()),
        ("INSERT INTO persona_events VALUES (3, 'other_profile', 'one', 'other', '', '')", ()),
    ])
    before = db.read_bytes()
    response = await call("/api/persona", "?session_id=one&events_limit=1")
    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["state"] == {"profile_id": "main_profile", "trust": 0.7}
    assert data["sessions"] == [{"profile_id": "main_profile", "session_id": "one", "mood_label": "calm"}]
    assert [row["id"] for row in data["events"]] == [1]
    assert b"hidden" not in response.body
    assert db.read_bytes() == before


@pytest.mark.asyncio
async def test_persona_tables_without_profile_are_not_exposed(isolated_runtime):
    db = isolated_runtime / "state" / "persona_state.db"
    seed_db(db, [
        ("CREATE TABLE persona_global_state (profile_id TEXT, trust REAL)", ()),
        ("INSERT INTO persona_global_state VALUES ('main_profile', 0.7)", ()),
        ("CREATE TABLE persona_session_state (session_id TEXT, mood_label TEXT)", ()),
        ("INSERT INTO persona_session_state VALUES ('someone-else', 'private')", ()),
    ])
    response = await call("/api/persona")
    assert response.status_code == 200
    assert json.loads(response.body)["sessions"] == []
    assert json.loads(response.body)["events"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("edge_columns", [("term_a", "term_b"), ("source", "target")])
async def test_word_map_accepts_aggregate_schema_without_bucket_id(isolated_runtime, edge_columns):
    db = isolated_runtime / "state" / "word_map.sqlite"
    source, target = edge_columns
    seed_db(db, [
        ("CREATE TABLE word_nodes (term TEXT, bucket_count INTEGER, weight REAL, raw_response TEXT)", ()),
        ("INSERT INTO word_nodes VALUES ('tea', 3, 2.0, 'hidden-raw')", ()),
        (f"CREATE TABLE word_edges ({source} TEXT, {target} TEXT, weight REAL)", ()),
        ("INSERT INTO word_edges VALUES ('tea', 'warm', 1.5)", ()),
    ])
    before = db.read_bytes()
    response = await call("/api/word-map")
    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["nodes"][0] == {"term": "tea", "kind": "", "bucket_count": 3, "weight": 2.0, "updated_at": ""}
    assert data["edges"][0] == {"term_a": "tea", "term_b": "warm", "bucket_count": 1, "weight": 1.5, "updated_at": ""}
    assert b"hidden" not in response.body
    assert db.read_bytes() == before


@pytest.mark.asyncio
async def test_word_map_preserves_per_bucket_aggregation_and_limits(isolated_runtime):
    db = isolated_runtime / "state" / "word_map.sqlite"
    seed_db(db, [
        ("CREATE TABLE word_card_nodes (term TEXT, kind TEXT, bucket_id TEXT, weight REAL, updated_at TEXT)", ()),
        ("INSERT INTO word_card_nodes VALUES ('tea', 'topic', 'a', 1, '2026-09-08')", ()),
        ("INSERT INTO word_card_nodes VALUES ('tea', 'topic', 'b', 2, '2026-09-09')", ()),
        ("INSERT INTO word_card_nodes VALUES ('quiet', 'topic', 'b', 1, '2026-09-09')", ()),
    ])
    response = await call("/api/word-map", "?nodes=1")
    assert json.loads(response.body)["nodes"] == [
        {"term": "tea", "kind": "topic", "bucket_count": 2, "weight": 3, "updated_at": "2026-09-09"},
    ]
    assert json.loads(response.body)["edges"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/api/reminders", "/api/persona", "/api/word-map",
    "/api/portrait-state",
])
async def test_missing_storage_is_explicit_and_never_created(isolated_runtime, path):
    response = await call(path)
    assert response.status_code == 503
    assert json.loads(response.body)["available"] is False
    assert list((isolated_runtime / "state").iterdir()) == []
    assert str(isolated_runtime).encode() not in response.body


@pytest.mark.asyncio
async def test_pending_not_yet_created_is_empty_without_writing(isolated_runtime):
    response = await call("/api/daily-chat-memory/pending")
    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["available"] is True
    assert data["storage_state"] == "not_created"
    assert data["count"] == 0 and data["items"] == []
    assert list((isolated_runtime / "state").iterdir()) == []


@pytest.mark.asyncio
async def test_pending_missing_state_directory_remains_unavailable(isolated_runtime, monkeypatch):
    monkeypatch.setitem(sh.config, "state_dir", str(isolated_runtime / "missing-volume"))
    response = await call("/api/daily-chat-memory/pending")
    assert response.status_code == 503
    assert json.loads(response.body)["available"] is False
    assert not (isolated_runtime / "missing-volume").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "[broken", '{"items": "hidden-malformed-value"}'])
async def test_malformed_pending_is_sanitized(isolated_runtime, text):
    path = isolated_runtime / "state" / "daily_chat_memory_candidates.json"
    path.write_text(text, encoding="utf-8")
    response = await call("/api/daily-chat-memory/pending")
    assert response.status_code == 500
    assert json.loads(response.body)["items"] == []
    assert b"hidden-malformed" not in response.body
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.asyncio
async def test_pending_and_portrait_expose_only_display_fields(isolated_runtime):
    pending = isolated_runtime / "state" / "daily_chat_memory_candidates.json"
    pending.write_text(json.dumps({"items": [{
        "id": "p1", "status": "pending", "raw_response": "hidden-raw",
        "candidate": {"title": "A day", "content": "warm", "source_content": "hidden-source"},
    }]}), encoding="utf-8")
    portrait = isolated_runtime / "state" / "portrait_state.json"
    portrait.write_text(json.dumps({
        "current_focus": "tea", "api_key": "hidden-key",
        "portrait": {"user": {"stable": "kind", "raw_response": "hidden-raw"}},
        "recent_activities": [{"text": "walk", "source_content": "hidden-source"}],
    }), encoding="utf-8")
    response = await call("/api/daily-chat-memory/pending")
    assert response.status_code == 200
    assert json.loads(response.body)["items"][0]["content"] == "warm"
    assert b"hidden" not in response.body
    response = await call("/api/portrait-state")
    assert response.status_code == 200
    assert json.loads(response.body)["portrait"]["user"]["stable"] == "kind"
    assert b"hidden" not in response.body


@pytest.mark.asyncio
async def test_moments_and_facts_accept_yaml_dates_lists_and_flags(isolated_runtime, monkeypatch):
    class Manager:
        async def list_all(self, include_archive):
            return [
                {"id": "m1", "content": "warm", "metadata": {"domain": ["daily_moment"], "date": date(2026, 9, 9)}},
                {"id": "f1", "content": "tea", "meta": {"type": ["profile_fact"], "created": date(2026, 9, 8), "active": "false"}},
            ]
    monkeypatch.setattr(sh, "bucket_mgr", Manager())
    response = await call("/api/moments")
    assert response.status_code == 200
    assert json.loads(response.body)["moments"][0]["date"] == "2026-09-09"
    response = await call("/api/profile-facts")
    assert response.status_code == 200
    assert json.loads(response.body)["facts"][0]["active"] is False


@pytest.mark.asyncio
async def test_dream_yaml_dates_and_quoted_false(isolated_runtime):
    dreams = isolated_runtime / "state" / "dreams"
    dreams.mkdir()
    (dreams / "dream_1.md").write_bytes(
        b'---\r\ndream_id: dream_1\r\nlocal_date: 2026-09-09\r\nsurfaced: "false"\r\ntags:\r\n  - warm\r\n---\r\nbody'
    )
    response = await call("/api/dreams/{dream_id}", path_params={"dream_id": "dream_1"})
    assert response.status_code == 200
    data = json.loads(response.body)
    assert data["local_date"] == "2026-09-09"
    assert data["dream_status"] == "latent"
    assert data["body"] == "body"


@pytest.mark.asyncio
@pytest.mark.parametrize("dream_id", ["..", "../secret", "..\\secret", "C:secret", "/secret"])
async def test_dream_detail_rejects_non_basename(isolated_runtime, dream_id):
    response = await call("/api/dreams/{dream_id}", path_params={"dream_id": dream_id})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_dream_detail_rejects_same_directory_symlink(isolated_runtime, monkeypatch):
    dreams = isolated_runtime / "state" / "dreams"
    dreams.mkdir()
    target = dreams / "dream_target.md"
    target.write_text("hidden-body", encoding="utf-8")
    link = dreams / "dream_link.md"
    try:
        link.symlink_to(target)
    except OSError:
        # Windows may not grant symlink creation. Exercise the rejection branch
        # with an existing file whose filesystem symlink predicate is replaced.
        link.write_text("hidden-body", encoding="utf-8")
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == link or original(path))
    response = await call("/api/dreams/{dream_id}", path_params={"dream_id": "dream_link"})
    assert response.status_code == 404
    assert b"hidden-body" not in response.body


@pytest.mark.asyncio
async def test_legacy_reminder_noop_does_not_write_or_expose_extra_columns(isolated_runtime):
    db = isolated_runtime / "state" / "reminders.sqlite"
    seed_db(db, [
        ("CREATE TABLE reminders (id TEXT, title TEXT, content TEXT, private_token TEXT)", ()),
        ("INSERT INTO reminders VALUES ('r1', 'Care', 'water', 'hidden-token')", ()),
    ])
    before = db.read_bytes()
    response = await call("/api/reminders/{reminder_id}", method="PATCH", body={"daily_limit": 2}, path_params={"reminder_id": "r1"})
    assert response.status_code == 200
    assert b"hidden" not in response.body
    assert db.read_bytes() == before


def test_default_paths_match_each_legacy_engine(isolated_runtime, monkeypatch):
    monkeypatch.delitem(sh.config, "state_dir")
    monkeypatch.chdir(isolated_runtime)
    expected = {
        "persona": "buckets/persona_state.db", "portrait": "state/portrait_state.json",
        "word_map": "buckets/state/word_map.sqlite", "reminders": "state/reminders.sqlite",
        "pending": "state/daily_chat_memory_candidates.json", "dreams": "dreams",
    }
    for kind, path in expected.items():
        assert compat._path_for(kind) == isolated_runtime / path
