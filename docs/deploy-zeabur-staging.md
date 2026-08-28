# Zeabur staging 部署（阶段 F）

更新时间：2026-08-28

## 目标

在用户已创建的 Zeabur `ombre` 项目中部署一个全新 staging 服务。该形态不触碰生产服务 `ombre-brain` 和 `xinchao-nian-caric`，也不复用生产卷。

## 拓扑

- Haven `server.py`：内部 `8000`，MCP、Dashboard、bucket API。
- Haven `gateway.py`：内部 `8010`，OpenAI/Anthropic 网关。
- Xinchao：内部 `18110`，动态状态、recent continuity、MCP。
- 入口代理：容器唯一公网端口 `9000`；Zeabur 注入的 `PORT` 会被复制为 `OMBRE_PROXY_PORT`。

公网路由：

| 路径 | 目标 |
| --- | --- |
| `/v1/...` | Haven Gateway |
| `/xinchao/...` | Xinchao（`/xinchao` 会被剥除） |
| 其他路径 | Haven MCP / Dashboard |

`entrypoint_zeabur.py` 同时启动四个进程；任一子进程退出就终止整组，便于 Zeabur 拉起新实例。

## Zeabur 设置

构建：选择 Dockerfile 路径 `Ombre-Brain/Dockerfile.zeabur`。Zeabur 的构建上下文需要是包含 `Ombre-Brain/` 和 `xinchao-nian/` 两个仓库的父目录；如果 Zeabur 只允许绑定单仓库，就先用这两个仓库组成一个部署仓库，不要用 `Ombre-Brain/Dockerfile`。

端口：`9000`。

Volume：

```text
/data
/state
```

`/data` 保存 bucket Markdown、embedding 索引、脱水缓存和 `gateway_state.db`。`/state` 保存 raw events、persona、portrait、dreams、reminders 和 Xinchao 短期状态。

## 环境变量

### 必填

```text
OMBRE_API_KEY=<dehydration model key>
OMBRE_EMBEDDING_API_KEY=<embedding key>
OMBRE_GATEWAY_TOKEN=<random 32+ chars>
SERVICE_TOKEN=<random 32+ chars>
MCP_PATH_TOKEN=<random 32+ chars>
OMBRE_MCP_TOKEN=<same value as MCP_PATH_TOKEN>
MCP_ENABLED=true
RECENT_CONTINUITY_ENABLED=true
```

### 模型

```text
OMBRE_BASE_URL=<OpenAI-compatible base URL>
OMBRE_DEHYDRATION_MODEL=<model>
OMBRE_EMBEDDING_BASE_URL=<embedding base URL>
OMBRE_EMBEDDING_MODEL=<embedding model>
OMBRE_GATEWAY_UPSTREAM_BASE_URL=<upstream base URL>
OMBRE_GATEWAY_UPSTREAM_MODEL=<default model>
OMBRE_GATEWAY_UPSTREAM_API_KEY=<upstream key>
```

### Xinchao

```text
OMBRE_MCP_URL=http://127.0.0.1:8000/mcp
XINCHAO_BASE_URL=http://127.0.0.1:18110
DASHBOARD_ENABLED=false
OAUTH_ENABLED=false
MODEL_ENABLED=false
BRIDGE_ENABLED=false
SHADOW_MODE=false
OMBRE_READ_ENABLED=true
OMBRE_WRITE_ENABLED=false
CONTEXT_OMBRE_ENABLED=true
```

### 可选

```text
OMBRE_MEMORY_WRITE_TOKEN=<random 32+ chars>
OMBRE_DASHBOARD_PASSWORD=<Dashboard first setup password>
```

## Runtime config

首次启动会自动创建 `/state/config.runtime.yaml`，内容不包含任何 token：

```yaml
gateway:
  xinchao_adapter:
    enabled: true
    base_url: "http://127.0.0.1:18110"
    service_token_env: "SERVICE_TOKEN"
    continuity_limit: 6
    notify_dynamic_state: true
    outbox_enabled: true
```

如果要改这份 runtime 配置，可先在 Zeabur Files/Console 删除它，再重启 staging 服务重新生成；也可以直接编辑后重启。不要在 YAML 中写 token。

## 数据预置

阶段 E 已生成的原文库在 `D:\silence\backups\stage-e-raw-events.sqlite`。部署成功、两个 Volume 挂载完成后，把它上传到 Zeabur 的 `/data/raw_events.sqlite`。不要把这份私密数据库放进 Git 或镜像。

842 条摘要的重写产物尚未完成，不要把 `stage-e-summary-artifact.json` 作为 staging 的长期记忆导入。

## 验收

```bash
curl https://<staging-domain>/health
curl https://<staging-domain>/v1/health
curl https://<staging-domain>/xinchao/health
```

前两个应返回 JSON；Xinchao 应返回 `system: xinchao-dynamic-mind`。之后再用真实客户端测 MCP 和一次带 `Authorization: Bearer <OMBRE_GATEWAY_TOKEN>` 的 `/v1/chat/completions` 请求。
