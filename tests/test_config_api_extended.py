from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace

import pytest


class FakeMCP:
    def __init__(self):
        self.routes = {}

    def custom_route(self, path, methods):
        def decorator(handler):
            for method in methods:
                self.routes[(path, method)] = handler
            return handler

        return decorator


class FakeRequest:
    def __init__(self, body=None):
        self.body = body

    async def json(self):
        return self.body


def _json(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("OMBRE_GATEWAY_ADMIN_URL", raising=False)
    monkeypatch.delenv("OMBRE_GATEWAY_TOKEN", raising=False)


def _install_routes(monkeypatch, tmp_path, config):
    import web.config_api as config_api

    mcp = FakeMCP()
    monkeypatch.setattr(config_api.sh, "config", config, raising=False)
    monkeypatch.setattr(config_api.sh, "dehydrator", SimpleNamespace(), raising=False)
    monkeypatch.setattr(config_api.sh, "embedding_engine", None, raising=False)
    monkeypatch.setattr(config_api.sh, "_require_auth", lambda request: None)
    monkeypatch.setattr(config_api.sh, "in_docker", lambda: False)
    monkeypatch.setattr(config_api, "read_config_yaml", lambda: copy.deepcopy(config))
    monkeypatch.setattr(
        config_api,
        "current_mcp_network_security",
        lambda *args, **kwargs: {"guard_active": False},
    )
    env_path = tmp_path / ".env"
    monkeypatch.setattr(config_api.sh, "_project_env_path", lambda: str(env_path))
    config_api.register(mcp)
    return config_api, mcp.routes[("/api/config", "GET")], mcp.routes[("/api/config", "POST")], env_path


def _base_config():
    return {
        "transport": "stdio",
        "mcp_require_auth": True,
        "mcp_auth_mode": "oauth",
        "buckets_dir": "buckets",
        "dehydration": {},
        "embedding": {"enabled": False},
        "gateway": {},
    }


def test_config_get_returns_extended_sections_and_masks_upstream_keys(monkeypatch, tmp_path):
    import web.config_api as config_api

    config = _base_config()
    config["gateway"] = {
        "retrieval_mode": "graph",
        "domain_sentinel_base_url": "https://sentinel.example",
        "domain_sentinel_api_key": "domain-secret",
        "upstreams": [
            {
                "name": "primary",
                "protocol": "openai",
                "base_url": "https://upstream.example/v1",
                "api_key_env": "UPSTREAM_KEY",
                "api_keys": [{"api_key": "upstream-secret"}],
                "models": ["model-a"],
            }
        ],
    }
    config.update(
        {
            "recall": {"query_resurface_enabled": True},
            "memory_diffusion": {"enabled": True, "max_hops": 3},
            "reranker": {"enabled": True, "api_key": "rerank-secret"},
            "persona": {"enabled": True},
            "dream": {"enabled": True},
            "reflection": {"enabled": True},
            "portrait": {"enabled": True},
            "self_anchor": {"entry_bucket_id": "anchor-1"},
        }
    )
    monkeypatch.setenv("UPSTREAM_KEY", "env-secret")
    monkeypatch.setenv("OMBRE_RERANKER_API_KEY", "rerank-secret")
    _, get_handler, _, _ = _install_routes(monkeypatch, tmp_path, config)
    payload = _json(__import__("asyncio").run(get_handler(FakeRequest())))

    assert all(name in payload for name in (
        "gateway", "recall", "memory_diffusion", "reranker", "persona",
        "dream", "reflection", "portrait", "self_anchor",
    ))
    gateway = payload["gateway"]
    upstream = gateway["upstreams"][0]
    assert upstream["api_key_envs"] == ["UPSTREAM_KEY"]
    assert upstream["key_count"] == 2
    assert upstream["ready"] is True
    assert "api_key" not in upstream
    assert "api_keys" not in upstream
    assert "has_direct_api_key" not in upstream
    assert gateway["domain_sentinel_api_key_masked"] == "doma...cret"
    assert "api_key" not in payload["reranker"]
    assert payload["reranker"]["api_key_masked"] == "rera...cret"


def test_config_post_merges_sections_persists_safe_yaml_and_hot_updates_gateway(monkeypatch, tmp_path):
    import web.config_api as config_api

    config = _base_config()
    _, _, post_handler, env_path = _install_routes(monkeypatch, tmp_path, config)
    monkeypatch.setenv("UPSTREAM_KEY", "")
    monkeypatch.setenv("OMBRE_DOMAIN_SENTINEL_API_KEY", "")
    monkeypatch.setenv("OMBRE_RERANKER_API_KEY", "")

    persisted = {}
    hot_payloads = []

    def atomic_update(mutate):
        mutate(persisted)
        return copy.deepcopy(persisted)

    async def hot_update(payload):
        hot_payloads.append(copy.deepcopy(payload))
        return "gateway_hot_reloaded"

    monkeypatch.setattr(config_api, "atomic_update_config_yaml", atomic_update)
    monkeypatch.setattr(config_api, "_hot_update_gateway_config", hot_update)

    body = {
        "persist": True,
        "persist_env": True,
        "gateway": {
            "retrieval_mode": "graph",
            "domain_sentinel_base_url": "https://sentinel.example",
            "domain_sentinel_api_key": "domain-secret",
            "upstreams": [{
                "name": "primary",
                "protocol": "claude",
                "base_url": "https://upstream.example",
                "api_key_env": "UPSTREAM_KEY",
                "api_key_values": ["upstream-secret"],
                "models": [{"id": "public-model", "upstream_model": "real-model"}],
            }],
        },
        "recall": {"query_resurface_enabled": True},
        "memory_diffusion": {"enabled": True, "max_hops": 3},
        "reranker": {"enabled": True, "api_key": "rerank-secret"},
        "persona": {"enabled": True},
        "dream": {"enabled": True},
        "reflection": {"enabled": True},
        "portrait": {"enabled": True},
        "self_anchor": {"entry_bucket_id": "anchor-1"},
    }
    response = __import__("asyncio").run(post_handler(FakeRequest(body)))
    payload = _json(response)

    assert response.status_code == 200
    assert config["recall"]["query_resurface_enabled"] is True
    assert config["gateway"]["upstreams"][0]["protocol"] == "anthropic"
    assert "api_keys" not in config["gateway"]["upstreams"][0]
    assert config["reranker"]["api_key"] == "rerank-secret"
    assert "persisted_to_yaml" in payload["updated"]
    assert "gateway_hot_reloaded" in payload["updated"]
    assert "gateway.domain_sentinel_api_key" in payload["updated"]
    assert "reranker.api_key" in payload["updated"]
    assert "env.UPSTREAM_KEY" in payload["updated"]
    assert "env.OMBRE_DOMAIN_SENTINEL_API_KEY" in payload["updated"]
    assert payload["restart_required"] is True
    assert hot_payloads and hot_payloads[0]["gateway"]["upstreams"][0]["api_keys"][0]["api_key"] == "upstream-secret"
    assert "UPSTREAM_KEY=upstream-secret" in env_path.read_text(encoding="utf-8")
    assert "OMBRE_DOMAIN_SENTINEL_API_KEY=domain-secret" in env_path.read_text(encoding="utf-8")

    def assert_no_secret_keys(value):
        if isinstance(value, dict):
            assert not {"api_key", "api_keys", "api_key_values"}.intersection(value)
            for child in value.values():
                assert_no_secret_keys(child)
        elif isinstance(value, list):
            for child in value:
                assert_no_secret_keys(child)

    assert_no_secret_keys(persisted)


def test_config_post_preserves_legacy_inline_keys_when_not_replaced(monkeypatch, tmp_path):
    import web.config_api as config_api

    config = _base_config()
    config["reranker"] = {"enabled": True, "api_key": "legacy-rerank-secret"}
    config["gateway"] = {
        "upstreams": [{
            "name": "primary",
            "base_url": "https://upstream.example",
            "api_key": "legacy-upstream-secret",
        }]
    }
    _, _, post_handler, _ = _install_routes(monkeypatch, tmp_path, config)

    persisted = copy.deepcopy(config)

    def atomic_update(mutate):
        mutate(persisted)
        return copy.deepcopy(persisted)

    monkeypatch.setattr(config_api, "atomic_update_config_yaml", atomic_update)
    response = __import__("asyncio").run(post_handler(FakeRequest({
        "persist": True,
        "reranker": {"enabled": False},
        "gateway": {"upstreams": [{"name": "primary", "base_url": "https://upstream.example"}]},
    })))

    assert response.status_code == 200
    assert persisted["reranker"]["api_key"] == "legacy-rerank-secret"
    assert persisted["gateway"]["upstreams"][0]["api_key"] == "legacy-upstream-secret"


def test_config_post_rejects_plaintext_keys_without_persist_env(monkeypatch, tmp_path):
    config = _base_config()
    _, _, post_handler, env_path = _install_routes(monkeypatch, tmp_path, config)
    before = copy.deepcopy(config)
    response = __import__("asyncio").run(post_handler(FakeRequest({
        "gateway": {"upstreams": [{
            "name": "primary",
            "base_url": "https://upstream.example",
            "api_key_env": "UPSTREAM_KEY",
            "api_key_values": ["secret"],
        }]},
    })))
    payload = _json(response)
    assert response.status_code == 400
    assert "persist_env=true" in payload["error"]
    assert config == before
    assert not env_path.exists()


def test_config_post_reports_gateway_warning_when_admin_channel_is_missing(monkeypatch, tmp_path):
    config = _base_config()
    _, _, post_handler, _ = _install_routes(monkeypatch, tmp_path, config)
    monkeypatch.delenv("OMBRE_GATEWAY_ADMIN_URL", raising=False)
    monkeypatch.delenv("OMBRE_GATEWAY_TOKEN", raising=False)

    response = __import__("asyncio").run(post_handler(FakeRequest({
        "gateway": {"retrieval_mode": "graph"},
    })))
    payload = _json(response)

    assert response.status_code == 200
    assert payload["ok"] is True
    assert "gateway_hot_reloaded" not in payload["updated"]
    assert any("Gateway 热更新未执行" in warning for warning in payload["warnings"])
    assert any("当前运行态" in warning for warning in payload["warnings"])


def test_config_post_rolls_back_runtime_and_env_when_yaml_persistence_fails(monkeypatch, tmp_path):
    import web.config_api as config_api

    config = _base_config()
    _, _, post_handler, env_path = _install_routes(monkeypatch, tmp_path, config)
    monkeypatch.setenv("UPSTREAM_KEY", "old-secret")
    env_path.write_text("KEEP=1\nUPSTREAM_KEY=old-secret\n", encoding="utf-8")
    before = copy.deepcopy(config)

    def failing_atomic(mutate):
        candidate = {}
        mutate(candidate)
        raise OSError("disk full")

    monkeypatch.setattr(config_api, "atomic_update_config_yaml", failing_atomic)
    response = __import__("asyncio").run(post_handler(FakeRequest({
        "persist": True,
        "persist_env": True,
        "gateway": {"upstreams": [{
            "name": "primary",
            "base_url": "https://upstream.example",
            "api_key_env": "UPSTREAM_KEY",
            "api_key_values": ["new-secret"],
        }]},
    })))
    payload = _json(response)
    assert response.status_code == 500
    assert payload["error"] == "persist failed"
    assert config == before
    assert __import__("os").environ["UPSTREAM_KEY"] == "old-secret"
    assert env_path.read_text(encoding="utf-8") == "KEEP=1\nUPSTREAM_KEY=old-secret\n"


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        ({"gateway": {"retrieval_mode": "invalid"}}, "gateway.retrieval_mode"),
        ({"memory_diffusion": {"max_hops": 99}}, "memory_diffusion.max_hops"),
        ({"gateway": {"upstreams": [
            {"name": "same"}, {"name": "same"}
        ]}}, "duplicate gateway upstream name"),
    ],
)
def test_config_post_rejects_invalid_extended_payload(monkeypatch, tmp_path, body, needle):
    config = _base_config()
    _, _, post_handler, _ = _install_routes(monkeypatch, tmp_path, config)
    response = __import__("asyncio").run(post_handler(FakeRequest(body)))
    payload = _json(response)
    assert response.status_code == 400
    assert needle in payload["error"]


def test_extended_env_writer_round_trips_loader_syntax_and_uses_selected_path(
    monkeypatch, tmp_path
):
    import env_loader
    import web.config_api as config_api

    env_path = tmp_path / "state" / "dashboard.env"
    monkeypatch.setattr(config_api.sh, "_project_env_path", lambda: str(env_path))
    monkeypatch.delenv("EXTENDED_TEST_KEY", raising=False)

    written = config_api._dashboard_write_env_updates(
        {"EXTENDED_TEST_KEY": 'value with #hash and "quote"'}
    )

    assert written == ["env.EXTENDED_TEST_KEY"]
    assert env_path.exists()
    assert env_path.read_text(encoding="utf-8") == (
        'EXTENDED_TEST_KEY="value with #hash and \\\"quote\\\""\n'
    )

    monkeypatch.delenv("EXTENDED_TEST_KEY", raising=False)
    assert env_loader.load_env_file(str(env_path)) == ["EXTENDED_TEST_KEY"]
    assert os.environ["EXTENDED_TEST_KEY"] == 'value with #hash and "quote"'


def test_blank_extended_keys_do_not_replace_legacy_inline_keys(
    monkeypatch, tmp_path
):
    config = _base_config()
    config["reranker"] = {"enabled": True, "api_key": "legacy-rerank-secret"}
    config["gateway"] = {
        "upstreams": [{
            "name": "primary",
            "base_url": "https://upstream.example",
            "api_key": "legacy-upstream-secret",
        }]
    }
    _, _, post_handler, env_path = _install_routes(monkeypatch, tmp_path, config)
    persisted = copy.deepcopy(config)

    def atomic_update(mutate):
        mutate(persisted)
        return copy.deepcopy(persisted)

    monkeypatch.setattr(__import__("web.config_api", fromlist=["atomic_update_config_yaml"]), "atomic_update_config_yaml", atomic_update)
    response = __import__("asyncio").run(post_handler(FakeRequest({
        "persist": True,
        "reranker": {"api_key": "   ", "enabled": False},
        "gateway": {"upstreams": [{
            "name": "primary",
            "base_url": "https://upstream.example",
            "api_key_env": "UPSTREAM_BLANK_KEY",
            "api_key_values": ["   "],
        }]},
    })))

    assert response.status_code == 200
    assert persisted["reranker"]["api_key"] == "legacy-rerank-secret"
    assert persisted["gateway"]["upstreams"][0]["api_key"] == "legacy-upstream-secret"
    assert not env_path.exists()


def test_get_masks_nested_secret_like_fields_and_reads_upstream_env_from_env_file(
    monkeypatch, tmp_path
):
    import web.config_api as config_api

    config = _base_config()
    config["gateway"] = {
        "domain_sentinel_base_url": "https://sentinel.example",
        "domain_sentinel_api_key": "domain-secret",
        "xinchao_adapter": {
            "enabled": True,
            "service_token": "sidecar-secret",
            "service_token_env": "SIDECAR_TOKEN",
        },
        "upstreams": [{
            "name": "file-backed",
            "base_url": "https://upstream.example",
            "api_key_envs": ["FILE_UPSTREAM_KEY"],
            "models": [{"id": "public", "upstream_model": "private-model"}],
        }],
    }
    env_path = tmp_path / ".env"
    env_path.write_text("FILE_UPSTREAM_KEY=file-secret\n", encoding="utf-8")
    _, get_handler, _, _ = _install_routes(monkeypatch, tmp_path, config)
    monkeypatch.delenv("FILE_UPSTREAM_KEY", raising=False)
    monkeypatch.delenv("SIDECAR_TOKEN", raising=False)
    monkeypatch.setattr(config_api.sh, "_read_env_var", lambda name: {
        "FILE_UPSTREAM_KEY": "file-secret",
    }.get(name, ""))

    payload = _json(__import__("asyncio").run(get_handler(FakeRequest())))
    upstream = payload["gateway"]["upstreams"][0]
    assert upstream["key_count"] == 1
    assert upstream["ready"] is True
    assert payload["gateway"]["xinchao_adapter"]["service_token_env"] == "SIDECAR_TOKEN"
    assert "service_token" not in payload["gateway"]["xinchao_adapter"]
    assert "domain_sentinel_api_key" not in payload["gateway"]
    assert "sidecar-secret" not in json.dumps(payload)


def test_config_post_persists_model_keys_to_env_and_removes_yaml_plaintext(
    monkeypatch, tmp_path
):
    import web.config_api as config_api

    config = _base_config()
    config["dehydration"] = {"api_key": "old-compress"}
    config["embedding"] = {"enabled": False, "api_key": "old-embed"}
    _, _, post_handler, env_path = _install_routes(monkeypatch, tmp_path, config)
    persisted = copy.deepcopy(config)

    def atomic_update(mutate):
        mutate(persisted)
        return copy.deepcopy(persisted)

    monkeypatch.setattr(config_api, "atomic_update_config_yaml", atomic_update)
    monkeypatch.setattr(config_api, "_rebuild_embedding_runtime", lambda: object())
    monkeypatch.setattr(
        config_api.sh,
        "dehydrator",
        SimpleNamespace(
            model="old", base_url="https://old", max_tokens=1024,
            temperature=0.1, timeout_seconds=60.0, api_format="openai_compat",
            api_key="old-compress", api_available=True, client=None,
        ),
    )
    response = __import__("asyncio").run(post_handler(FakeRequest({
        "persist": True,
        "persist_env": True,
        "dehydration": {"api_key": "new-compress"},
        "embedding": {"api_key": "new-embed", "enabled": False},
    })))
    payload = _json(response)

    assert response.status_code == 200
    assert os.environ["OMBRE_COMPRESS_API_KEY"] == "new-compress"
    assert os.environ["OMBRE_EMBED_API_KEY"] == "new-embed"
    assert persisted["dehydration"].get("api_key") is None
    assert persisted["embedding"].get("api_key") is None
    assert "OMBRE_COMPRESS_API_KEY=new-compress" in env_path.read_text(encoding="utf-8")
    assert "OMBRE_EMBED_API_KEY=new-embed" in env_path.read_text(encoding="utf-8")
    assert "env.OMBRE_COMPRESS_API_KEY" in payload["updated"]
    assert "env.OMBRE_EMBED_API_KEY" in payload["updated"]


def test_config_post_does_not_leave_env_or_runtime_mutation_when_env_write_fails(
    monkeypatch, tmp_path
):
    import web.config_api as config_api

    config = _base_config()
    config["gateway"] = {"upstreams": []}
    _, _, post_handler, env_path = _install_routes(monkeypatch, tmp_path, config)
    before = copy.deepcopy(config)

    def fail_env(_updates):
        raise OSError("read-only env mount")

    monkeypatch.setattr(config_api, "_dashboard_write_env_updates", fail_env)
    response = __import__("asyncio").run(post_handler(FakeRequest({
        "persist": True,
        "persist_env": True,
        "gateway": {
            "upstreams": [{
                "name": "primary",
                "base_url": "https://upstream.example",
                "api_key_env": "ENV_WRITE_FAIL_KEY",
                "api_key_values": ["new-secret"],
            }]
        },
    })))
    payload = _json(response)

    assert response.status_code == 500
    assert payload["error"] == "environment persistence failed"
    assert config == before
    assert os.environ.get("ENV_WRITE_FAIL_KEY") is None
    assert not env_path.exists()


def capture_persistence(monkeypatch, config_api, config):
    persisted = copy.deepcopy(config)

    def atomic_update(mutate):
        candidate = copy.deepcopy(persisted)
        mutate(candidate)
        persisted.clear()
        persisted.update(candidate)
        return copy.deepcopy(persisted)

    monkeypatch.setattr(config_api, "atomic_update_config_yaml", atomic_update)
    return persisted


def test_blank_embedding_key_preserves_runtime_and_yaml(monkeypatch, tmp_path):
    config = _base_config()
    config["embedding"]["api_key"] = "old-embedding-key"
    module, _, post, env_path = _install_routes(monkeypatch, tmp_path, config)
    persisted = capture_persistence(monkeypatch, module, config)
    monkeypatch.setattr(module, "_rebuild_embedding_runtime", lambda: None)
    response = __import__("asyncio").run(post(FakeRequest({
        "persist": True, "embedding": {"api_key": "   ", "model": "changed"},
    })))
    assert response.status_code == 200
    assert config["embedding"]["api_key"] == "old-embedding-key"
    assert persisted["embedding"]["api_key"] == "old-embedding-key"
    assert not env_path.exists()


def test_new_named_upstream_never_inherits_previous_position_key(monkeypatch, tmp_path):
    config = _base_config()
    config["gateway"]["upstreams"] = [{"name": "old", "base_url": "https://old.example", "api_key": "private-old-key"}]
    module, _, post, _ = _install_routes(monkeypatch, tmp_path, config)
    persisted = capture_persistence(monkeypatch, module, config)
    response = __import__("asyncio").run(post(FakeRequest({
        "persist": True,
        "gateway": {"upstreams": [{"name": "new", "base_url": "https://new.example"}]},
    })))
    assert response.status_code == 200
    assert "api_key" not in config["gateway"]["upstreams"][0]
    assert "api_key" not in persisted["gateway"]["upstreams"][0]


def test_explicit_inline_clear_matches_runtime_yaml_and_gateway(monkeypatch, tmp_path):
    config = _base_config()
    config["gateway"]["upstreams"] = [{"name": "primary", "api_key": "old-inline", "api_keys": ["old-pool"]}]
    module, _, post, _ = _install_routes(monkeypatch, tmp_path, config)
    persisted = capture_persistence(monkeypatch, module, config)
    hot_payloads = []

    async def hot(payload):
        hot_payloads.append(payload)
        return "gateway_hot_reloaded"

    monkeypatch.setattr(module, "_hot_update_gateway_config", hot)
    response = __import__("asyncio").run(post(FakeRequest({
        "persist": True, "gateway": {"upstreams": [{"name": "primary", "clear_api_key": True}]},
    })))
    assert response.status_code == 200
    for candidate in (config, persisted):
        row = candidate["gateway"]["upstreams"][0]
        assert "api_key" not in row and "api_keys" not in row
    row = hot_payloads[0]["gateway"]["upstreams"][0]
    assert row["api_key"] == ""
    assert row["api_keys"] == []


def test_key_rotation_materializes_unchanged_pool_keys_for_gateway(monkeypatch, tmp_path):
    config = _base_config()
    config["gateway"]["upstreams"] = [{
        "name": "primary", "api_key": "old-inline", "api_key_envs": ["ROTATE_A", "ROTATE_B"],
    }]
    module, _, post, _ = _install_routes(monkeypatch, tmp_path, config)
    capture_persistence(monkeypatch, module, config)
    monkeypatch.setenv("ROTATE_A", "old-a")
    monkeypatch.setenv("ROTATE_B", "keep-b")
    monkeypatch.setattr(module.sh, "_read_env_var", lambda key: os.environ.get(key, ""))
    hot_payloads = []

    async def hot(payload):
        hot_payloads.append(payload)
        return "gateway_hot_reloaded"

    monkeypatch.setattr(module, "_hot_update_gateway_config", hot)
    response = __import__("asyncio").run(post(FakeRequest({
        "persist": True, "persist_env": True,
        "gateway": {"upstreams": [{"name": "primary", "api_key_values": ["new-a"]}]},
    })))
    assert response.status_code == 200
    row = hot_payloads[0]["gateway"]["upstreams"][0]
    assert [entry["api_key"] for entry in row["api_keys"]] == ["new-a", "keep-b"]
    assert row["api_key"] == ""
    assert not row.get("api_key_envs") and not row.get("api_key_env")
    assert config["gateway"]["upstreams"][0]["api_key_envs"] == ["ROTATE_A", "ROTATE_B"]


def test_nested_partial_settings_preserve_unedited_values(monkeypatch, tmp_path):
    config = _base_config()
    config["gateway"]["xinchao_adapter"] = {"enabled": True, "service_token": "existing-token", "timeout": 4}
    module, get, post, _ = _install_routes(monkeypatch, tmp_path, config)
    persisted = capture_persistence(monkeypatch, module, config)
    response = __import__("asyncio").run(post(FakeRequest({
        "persist": True, "gateway": {"xinchao_adapter": {"enabled": False}},
    })))
    assert response.status_code == 200
    for candidate in (config, persisted):
        assert candidate["gateway"]["xinchao_adapter"] == {"enabled": False, "service_token": "existing-token", "timeout": 4}
    response = __import__("asyncio").run(get(FakeRequest()))
    assert b"existing-token" not in response.body


def test_explicit_model_clear_is_persistent_and_blank_is_not_clear(monkeypatch, tmp_path):
    config = _base_config()
    config["embedding"]["api_key"] = "old-inline"
    module, _, post, env_path = _install_routes(monkeypatch, tmp_path, config)
    persisted = capture_persistence(monkeypatch, module, config)
    monkeypatch.setenv("OMBRE_EMBED_API_KEY", "old-env")
    env_path.write_text("KEEP=1\nOMBRE_EMBED_API_KEY=old-env\n", encoding="utf-8")
    monkeypatch.setattr(module, "_rebuild_embedding_runtime", lambda: None)
    request = {"persist_env": True, "embedding": {"clear_api_key": True}}
    response = __import__("asyncio").run(post(FakeRequest(request)))
    assert response.status_code == 400
    assert config["embedding"]["api_key"] == "old-inline"
    assert os.environ["OMBRE_EMBED_API_KEY"] == "old-env"
    request["persist"] = True
    response = __import__("asyncio").run(post(FakeRequest(request)))
    assert response.status_code == 200
    assert "api_key" not in persisted["embedding"]
    assert "api_key" not in config["embedding"]
    assert "OMBRE_EMBED_API_KEY" not in os.environ
    assert env_path.read_text(encoding="utf-8") == "KEEP=1\nOMBRE_EMBED_API_KEY=\n"


def test_failed_yaml_verification_rolls_back_all_files_and_runtime(monkeypatch, tmp_path):
    import utils

    config = _base_config()
    config["reranker"] = {"enabled": False, "api_key": "old-inline"}
    module, _, post, env_path = _install_routes(monkeypatch, tmp_path, config)
    config_path = tmp_path / "config.yaml"
    original_yaml = b"# original\r\nreranker:\r\n  enabled: false\r\n  api_key: old-inline\r\n"
    config_path.write_bytes(original_yaml)
    original_env = b"KEEP=1\r\nOMBRE_RERANKER_API_KEY=old-env\r\n"
    env_path.write_bytes(original_env)
    monkeypatch.setenv("OMBRE_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("OMBRE_RERANKER_API_KEY", "old-env")
    before = copy.deepcopy(config)
    real_load = utils.yaml.safe_load

    def mismatch(source):
        value = real_load(source)
        return {} if value.get("reranker", {}).get("enabled") else value

    monkeypatch.setattr(utils.yaml, "safe_load", mismatch)
    response = __import__("asyncio").run(post(FakeRequest({
        "persist": True, "persist_env": True, "reranker": {"enabled": True, "api_key": "new-key"},
    })))
    assert response.status_code == 500
    assert _json(response)["updated"] == []
    assert config == before
    assert config_path.read_bytes() == original_yaml
    assert env_path.read_bytes() == original_env
    assert os.environ["OMBRE_RERANKER_API_KEY"] == "old-env"


def test_config_read_error_never_returns_yaml_or_path_details(monkeypatch, tmp_path):
    import yaml

    module, get, _, _ = _install_routes(monkeypatch, tmp_path, _base_config())

    def broken_yaml():
        raise yaml.YAMLError("secret-from-yaml at private-path")

    monkeypatch.setattr(module, "read_config_yaml", broken_yaml)
    response = __import__("asyncio").run(get(FakeRequest()))
    assert response.status_code == 500
    assert _json(response) == {"error": "failed to read persisted config"}


def test_concurrent_config_reads_and_writes_share_commit_lock(monkeypatch, tmp_path):
    import asyncio

    config = _base_config()
    module, get, post, _ = _install_routes(monkeypatch, tmp_path, config)
    persisted = capture_persistence(monkeypatch, module, config)
    commits = []

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hot(payload):
            commits.append(copy.deepcopy(payload))
            if len(commits) == 1:
                entered.set()
                await release.wait()
            return "gateway_hot_reloaded"

        monkeypatch.setattr(module, "_hot_update_gateway_config", hot)
        first = asyncio.create_task(post(FakeRequest({"persist": True, "gateway": {"retrieval_mode": "graph"}})))
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = asyncio.create_task(post(FakeRequest({"persist": True, "gateway": {"future_setting": 2}})))
        reader = asyncio.create_task(get(FakeRequest()))
        try:
            await asyncio.sleep(0.025)
            assert not second.done() and not reader.done()
            assert len(commits) == 1
        finally:
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(first, second, reader), timeout=2)
        assert all(response.status_code == 200 for response in responses)

    asyncio.run(scenario())
    assert config["gateway"] == persisted["gateway"] == {"retrieval_mode": "graph", "future_setting": 2}
