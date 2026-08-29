# ============================================================
# Module: Env File Loader (env_loader.py)
# 模块：环境变量文件加载
#
# Resolves and loads the dashboard-managed .env file into os.environ.
# 解析并加载面板写入的 .env 文件到 os.environ
#
# Depended on by: entrypoint_zeabur.py, server.py, gateway.py
# 被谁依赖：entrypoint_zeabur.py, server.py, gateway.py
# ============================================================

import os
import re
import logging


def ombre_env_path() -> str:
    """Resolve the dashboard-managed .env path.

    Falls back to the state dir rather than the source dir: /app belongs to the
    image layer and is recreated on every deploy, so keys written there vanish.
    """
    explicit = os.environ.get("OMBRE_ENV_PATH", "").strip()
    if explicit:
        return explicit
    state_dir = os.environ.get("OMBRE_STATE_DIR", "").strip()
    if state_dir:
        return os.path.join(state_dir, ".env")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def parse_env_line(line: str) -> "tuple[str, str] | None":
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", stripped)
    if not match:
        return None
    key = match.group(1)
    raw = match.group(2).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        quote = raw[0]
        body = raw[1:-1]
        if quote == '"':
            body = body.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        return key, body
    return key, raw


def load_env_file(path: "str | None" = None, *, override: bool = True) -> "list[str]":
    """Load the dashboard-managed .env into os.environ.

    Must run before config loading and before sidecars are spawned, otherwise
    the gateway and xinchao processes never see keys saved from the dashboard.
    Returns the variable names applied; values are never returned or logged.
    """
    env_path = path or ombre_env_path()
    try:
        if not os.path.isfile(env_path):
            return []
        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        logging.warning(f"Failed to read env file / 读取 env 文件失败: {env_path}: {e}")
        return []

    applied = []
    for line in content.splitlines():
        parsed = parse_env_line(line)
        if not parsed:
            continue
        key, value = parsed
        if not value:
            continue
        if not override and os.environ.get(key):
            continue
        os.environ[key] = value
        applied.append(key)
    return applied