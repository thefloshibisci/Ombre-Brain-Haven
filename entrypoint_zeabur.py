"""Zeabur staging entrypoint for Haven + Gateway + Xinchao sidecar."""

from pathlib import Path

import os
import signal
import subprocess
import sys
import time
import yaml


SHUTDOWN_GRACE_SECONDS = 10


def ensure_runtime_config() -> None:
    state_dir = Path(os.environ.get("OMBRE_STATE_DIR", "/state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime_path = state_dir / "config.runtime.yaml"
    if runtime_path.exists():
        return

    # No credentials here: the adapter resolves SERVICE_TOKEN from env.
    config = {
        "gateway": {
            "xinchao_adapter": {
                "enabled": True,
                "base_url": "http://127.0.0.1:18110",
                "service_token_env": "SERVICE_TOKEN",
                "continuity_limit": 6,
                "notify_dynamic_state": True,
                "outbox_enabled": True,
            }
        },
        "raw_events": {
            "db_path": "/data/raw_events.sqlite"
        },
    }
    runtime_path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def main() -> int:
    port = os.environ.get("PORT", "9000")
    os.environ["PORT"] = port
    os.environ["OMBRE_PROXY_PORT"] = port
    os.environ.setdefault("OMBRE_GATEWAY_PORT", "8010")
    os.environ.setdefault("OMBRE_XINCHAO_PORT", "18110")
    os.environ.setdefault("OMBRE_TRANSPORT", "streamable-http")
    os.environ.setdefault("OMBRE_BUCKETS_DIR", "/data")
    os.environ.setdefault("STATE_PATH", "/state/xinchao-state.json")
    os.environ.setdefault(
        "TRANSITION_JOURNAL_PATH", "/state/xinchao-transitions.jsonl"
    )
    os.environ.setdefault(
        "RECENT_CONTINUITY_STATE_PATH", "/state/recent-continuity.json"
    )
    os.environ.setdefault("OAUTH_STATE_PATH", "/state/xinchao-oauth.json")
    os.environ.setdefault("BRIDGE_STATE_PATH", "/state/xinchao-bridge-queue.json")
    os.environ.setdefault("CABIN_STATE_PATH", "/state/xinchao-cabin.json")

    ensure_runtime_config()

    processes: list[subprocess.Popen] = []

    def terminate(signum, frame):
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)

    commands = [
        [sys.executable, "proxy_server.py"],
        [sys.executable, "server.py"],
        [sys.executable, "gateway.py"],
        ["node", "xinchao/src/server.js"],
    ]
    for command in commands:
        process = subprocess.Popen(command, cwd="/app")
        processes.append(process)
        if process.poll() is not None:
            for other in processes:
                if other.poll() is None:
                    other.terminate()
            return process.returncode or 1

    try:
        while True:
            for process in processes:
                code = process.poll()
                if code is not None:
                    for other in processes:
                        if other.poll() is None:
                            other.terminate()
                    return code or 1
            time.sleep(1)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        deadline = time.time() + SHUTDOWN_GRACE_SECONDS
        for process in processes:
            try:
                process.wait(timeout=max(0, deadline - time.time()))
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
