"""Contract tests for env_loader / 环境变量加载合同测试.

Keys saved from the dashboard must land on the persistent state volume and be
readable by a freshly spawned process. /app belongs to the image layer and is
recreated on every deploy, so anything written there is lost.
"""

import os
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from env_loader import load_env_file, ombre_env_path, parse_env_line  # noqa: E402


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("OMBRE_ENV_PATH", raising=False)
    monkeypatch.delenv("OMBRE_STATE_DIR", raising=False)


def test_path_prefers_explicit_override(monkeypatch, tmp_path):
    explicit = str(tmp_path / "custom.env")
    monkeypatch.setenv("OMBRE_ENV_PATH", explicit)
    monkeypatch.setenv("OMBRE_STATE_DIR", str(tmp_path / "state"))
    assert ombre_env_path() == explicit


def test_path_falls_back_to_state_dir_not_source_dir(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("OMBRE_STATE_DIR", str(state_dir))
    resolved = ombre_env_path()
    assert resolved == os.path.join(str(state_dir), ".env")
    assert not resolved.startswith(REPO_ROOT), "secrets must not resolve into the image layer"


def test_path_last_resort_is_source_dir():
    assert ombre_env_path() == os.path.join(REPO_ROOT, ".env")


@pytest.mark.parametrize(
    "line,expected",
    [
        ("KEY=value", ("KEY", "value")),
        ("export KEY=value", ("KEY", "value")),
        ("  KEY = value  ", ("KEY", "value")),
        ("KEY='va lue'", ("KEY", "va lue")),
        ('KEY="va#lue"', ("KEY", "va#lue")),
        ('KEY="a\\nb"', ("KEY", "a\nb")),
        ("KEY=sk-a=b=c", ("KEY", "sk-a=b=c")),
        ("# comment", None),
        ("", None),
        ("not an assignment", None),
        ("1BAD=x", None),
    ],
)
def test_parse_env_line(line, expected):
    assert parse_env_line(line) == expected


def test_load_applies_values_and_returns_names_only(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OMBRE_TEST_KEY=sk-secret-value\nEMPTY=\n", encoding="utf-8")
    monkeypatch.delenv("OMBRE_TEST_KEY", raising=False)

    applied = load_env_file(str(env_file))

    assert applied == ["OMBRE_TEST_KEY"], "empty values must not be applied"
    assert os.environ["OMBRE_TEST_KEY"] == "sk-secret-value"
    assert all("sk-secret-value" not in name for name in applied)


def test_load_missing_file_is_silent(tmp_path):
    assert load_env_file(str(tmp_path / "absent.env")) == []


def test_override_flag(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OMBRE_TEST_OVERRIDE=from-file\n", encoding="utf-8")

    monkeypatch.setenv("OMBRE_TEST_OVERRIDE", "from-platform")
    assert load_env_file(str(env_file), override=False) == []
    assert os.environ["OMBRE_TEST_OVERRIDE"] == "from-platform"

    assert load_env_file(str(env_file)) == ["OMBRE_TEST_OVERRIDE"]
    assert os.environ["OMBRE_TEST_OVERRIDE"] == "from-file"


def test_fresh_process_reads_keys_from_state_dir(tmp_path):
    """A newly spawned sidecar must see keys written by an earlier process."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / ".env").write_text(
        "OMBRE_API_KEY=sk-alpha\nOMBRE_GATEWAY_PROVIDER_API_KEY_1=sk-beta\n",
        encoding="utf-8",
    )

    script = textwrap.dedent(
        """
        import json, os, sys
        sys.path.insert(0, sys.argv[1])
        from env_loader import load_env_file
        names = load_env_file()
        print(json.dumps({
            "names": names,
            "alpha": os.environ.get("OMBRE_API_KEY"),
            "beta": os.environ.get("OMBRE_GATEWAY_PROVIDER_API_KEY_1"),
        }))
        """
    )

    child_env = dict(os.environ)
    child_env["OMBRE_STATE_DIR"] = str(state_dir)
    child_env.pop("OMBRE_ENV_PATH", None)
    child_env.pop("OMBRE_API_KEY", None)
    child_env.pop("OMBRE_GATEWAY_PROVIDER_API_KEY_1", None)

    out = subprocess.run(
        [sys.executable, "-c", script, REPO_ROOT],
        capture_output=True,
        text=True,
        env=child_env,
        check=True,
    )
    payload = __import__("json").loads(out.stdout.strip().splitlines()[-1])
    assert sorted(payload["names"]) == [
        "OMBRE_API_KEY",
        "OMBRE_GATEWAY_PROVIDER_API_KEY_1",
    ]
    assert payload["alpha"] == "sk-alpha"
    assert payload["beta"] == "sk-beta"


def test_writer_and_parser_round_trip(monkeypatch, tmp_path):
    """server.py's writer output must be readable back by the parser."""
    import server

    env_file = tmp_path / ".env"
    monkeypatch.setenv("OMBRE_ENV_PATH", str(env_file))

    values = {
        "OMBRE_API_KEY": "sk-plain",
        "OMBRE_REFLECTION_API_KEY": "sk with space",
        "OMBRE_GATEWAY_PROVIDER_API_KEY_1": "sk-with#hash",
        "OMBRE_GATEWAY_PROVIDER_API_KEY_2": 'sk-with"quote',
        "OMBRE_GATEWAY_PROVIDER_API_KEY_3": "sk-with=equals",
    }
    server._write_dashboard_env_values(values)

    for key in values:
        monkeypatch.delenv(key, raising=False)
    load_env_file(str(env_file))

    for key, expected in values.items():
        assert os.environ[key] == expected, f"round trip failed for {key}"