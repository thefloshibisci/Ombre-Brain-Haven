# Gateway ↔ Xinchao 运行时合同

版本：`gateway-xinchao-contract/v1`
更新时间：2026-08-26
状态：阶段 A 锁定；阶段 C/D 实现目标

## 1. 职责和禁止项

- Haven Gateway：公共聊天 API、上游模型路由、Prompt Cache、同 session 短期状态、Haven 召回与最终上下文组装。
- Xinchao：动态状态、短期 recent continuity、handoff、跨客户端 profile/session 共享、MCP/OAuth。
- Haven Brain：长期 bucket、raw events、Source、media、长期召回。

禁止：

- Xinchao 保存长期 bucket 副本；
- Gateway 请求 Xinchao 时触发 Xinchao 再读取 Haven；
- Gateway 通过 MCP-over-MCP 写 continuity；
- Xinchao 失败导致聊天接口 5xx；
- system/tool/hidden reasoning、凭据或 Gateway 注入块进入 continuity/raw events；
- 将 continuity API 响应的完整 JSON 直接塞入 prompt。

## 2. 配置合同

Haven `config.yaml` 新增以下可选配置。缺省时完全保持旧 Gateway 行为：

```yaml
gateway:
  xinchao_adapter:
    enabled: false
    base_url: ""
    service_token_env: "XINCHAO_SERVICE_TOKEN"
    context_max_tokens: 600
    read_timeout_seconds: 1.5
    write_timeout_seconds: 2.0
    continuity_limit: 6
    notify_dynamic_state: true
    outbox_enabled: true
    outbox_max_attempts: 8
    outbox_max_age_hours: 72
    outbox_poll_seconds: 5
    session_id_max_length: 160
```

约束：

- token 仅从环境变量读取，不接受 YAML 明文；
- `enabled=true` 时才解析/使用 base URL；
- health/debug 只报告 token 是否配置，不回显值；
- URL 必须是明确的实验内网 origin；生产地址扫描必须拒绝已知生产 host；
- adapter 初始化失败不得阻止 Gateway 启动，状态记为 degraded。

## 3. 身份和 session 映射

输入：

- Gateway header `X-Ombre-Session-Id`；
- Gateway 现有客户端识别结果 `client_label`；
- Xinchao 自身配置 `RECENT_CONTINUITY_PROFILE_ID`。

映射：

```text
gateway:<normalized-client-label>:<normalized-gateway-session-id>
```

规范化：

1. client/session 只来自请求 metadata/header，不从模型正文推断；
2. trim；控制字符拒绝；空 session 继续使用 Gateway 既有 default session；
3. 允许字母、数字、`._:-`，其他字符用单个 `-` 替换；
4. 若结果长度超过 `session_id_max_length`：保留可读前缀并追加 `:` + SHA-256 前 24 个十六进制字符；
5. 相同输入永远产生相同映射，不使用随机数或时间戳。

不同客户端使用不同 session，但 Xinchao 服务端 profile ID 相同，从而允许跨端 continuity；Xinchao 必须继续按 profile 隔离其他用户/AI 关系空间。

## 4. Xinchao continuity 写接口

### Endpoint

```http
POST /v1/continuity/sync
Authorization: Bearer <SERVICE_TOKEN>
Content-Type: application/json
```

### 请求

```json
{
  "session_id": "gateway:desktop:main",
  "client": "gateway-desktop",
  "messages": [
    {"turn_id": "<stable>:user", "role": "user", "text": "..."},
    {"turn_id": "<stable>:assistant", "role": "assistant", "text": "..."}
  ],
  "limit": 6
}
```

校验：

- 复用 MCP `continuitySyncArgs()` 对应的规范化规则和 `synchronizeRecentContinuity()`；
- 只接受 `user`、`assistant`；
- 每次最多接受配置允许的 bounded message 数；
- `turn_id`、session、client、text 必须有长度上限；
- 拒绝 system/tool/function/developer；
- 拒绝或清理凭据模式、`Xinchao Recent Context`、`Recent Context`、`Recalled Memory`、`Core Memory` 等注入块；
- 一个请求中的合法项可部分成功，响应必须给 accepted/duplicates；结构级非法请求返回 400；鉴权失败返回 401。

### 响应

```json
{
  "accepted": 2,
  "duplicates": 0,
  "text": "其他 session 的近期连续性"
}
```

`text` 必须是现有 `synchronizeRecentContinuity()` 的有界结果，不新增第二个 store。HTTP 和 MCP 必须在测试中证明共用同一函数/幂等语义。

## 5. Gateway context 读取

### Endpoint

```http
GET /v1/context?session_id=<mapped>&mode=turn&max_tokens=<budget>
Authorization: Bearer <SERVICE_TOKEN>
```

固定规则：

- `mode=turn`，禁止 `session_start`；
- 默认总预算 600 tokens，可配置但必须有硬上限；
- 请求路径超时 1.5 秒；不自动重试；
- 只消费允许 section：dynamic state、handoff、recent continuity；
- 忽略 envelope 审计、revision、digest、内部 warnings 正文；
- 只生成一个 `Xinchao Recent Context` 动态块；
- 当前请求已经包含的消息按 `turn_id` 优先、规范化文本 hash 次优去重；
- 无 section 或 `delivered=false` 时不注入空块。

建议内部返回类型：

```python
@dataclass
class XinchaoContext:
    text: str
    estimated_tokens: int
    section_count: int
    degraded: bool
    warnings: tuple[str, ...]
```

## 6. 动态事件接口

Gateway 仅在完整成功轮次后调用现有：

```http
POST /v1/conversation-event
Authorization: Bearer <SERVICE_TOKEN>
```

最小 payload：

```json
{
  "event_id": "<stable-event-id>",
  "session_id": "gateway:desktop:main",
  "tone": "focused"
}
```

规则：

- 默认不发送 `interaction_type`；
- 不自动猜 affection、intimacy、conflict、loss 等高语义类型；
- 客户端明确、可信地提供时，仅透传服务端已有白名单；
- Xinchao 按 `event_id` 幂等；重复投递不得重复增加动态反馈。

## 7. 稳定事件 ID

Gateway round 必须先有稳定 round identity。事件基材：

```text
profile_id\0gateway_session_id\0round_id\0role\0route\0sha256(normalized_visible_text)
```

事件 ID：

```text
ombre-gw-v1:<sha256(material)>
```

- continuity turn 使用相同基 ID加 `:user` / `:assistant`，或把 role 包含在 material 中；
- dynamic event 使用 round 级 material，不含 wall-clock timestamp；
- normalization 仅统一换行、Unicode NFC、首尾空白，不进行语义改写；
- profile ID 不写入日志正文，只可进入 hash material。

## 8. 成功轮次写入顺序

只有完整有效 assistant 可见文本产生后：

1. `RawEventStore.ingest()` 写 user/assistant 两条 raw events；
2. `GatewayState` 写入同 session `conversation_turns`；
3. 同一 SQLite transaction 创建 continuity outbox 项和可选 dynamic event outbox 项；
4. 后台 worker 读取 outbox 并投递 Xinchao；
5. 成功后标记 delivered；冲突/duplicate 也视为幂等成功。

流式合同：

- 只在上游流正常结束并完成可见文本聚合后执行上述步骤；
- 客户端中断、上游异常、空 assistant、仅 tool call 的轮次不写 continuity；
- tool call/tool result/hidden reasoning 不写 raw events 或 continuity；
- 已完整结束但客户端断开后的服务端处理是否继续，沿用 Haven 现有完整响应判定，不得产生半文本。

## 9. Outbox 合同

建议在 `gateway_state.db` 增加：

```sql
CREATE TABLE xinchao_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK(kind IN ('continuity','conversation_event')),
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT NOT NULL DEFAULT '',
  last_error_code TEXT NOT NULL DEFAULT '',
  last_error_at TEXT NOT NULL DEFAULT ''
);
```

行为：

- enqueue 使用 `event_key` 唯一约束；
- 每次 claim 有 lease，避免多个 worker 重复并发投递；
- 指数退避带上限和轻微 jitter；
- 4xx 中鉴权/结构错误标记 failed，不无限重试；429/5xx/timeout 可重试；
- 超过 max attempts 或 max age 转 failed；
- 错误正文脱敏且截断；
- Gateway 退出不丢 pending；恢复后继续；
- adapter disabled 时不创建 outbox。

## 10. 请求前上下文顺序与去重

优先级从高到低：

1. 当前请求真实 user/assistant 消息；
2. 同 session Just Now；
3. Xinchao recent continuity/handoff；
4. Haven bucket recall 与 raw evidence；
5. Persona/Xinchao 动态语气；
6. graph/Word Map/affect hints。

去重：

- 先用稳定 event/turn ID；
- 再用规范化全文 SHA-256；
- 最后使用 Haven 现有 semantic/lexical session dedupe；
- 相同内容只保留最高优先级层；
- raw evidence 只在明确日期/原句/细节路径出现，不能作为普通对话默认历史。

Prompt Cache：

- 稳定 system/tools 在前；
- 动态 Xinchao、Just Now、recall 放在缓存稳定区之后；
- 不修改客户端模型名或既有 route；
- OpenAI `prompt_cache_key` 与 Anthropic cache control 继续由 Haven 现有逻辑生成。

## 11. 降级矩阵

| 故障 | 请求路径 | 写路径 | 用户可见结果 |
|---|---|---|---|
| Xinchao context timeout | 跳过 Xinchao，使用同 session local turns | 后续仍入 outbox | 聊天继续，不返回 5xx |
| Xinchao 401/403 | 标记 adapter auth degraded | outbox failed/auth | 聊天继续，诊断提示认证失败但不泄密 |
| Xinchao 429/5xx | 跳过本轮 context | 指数退避 | 聊天继续 |
| malformed context envelope | fail closed，不注入 | 写 outbox 不受影响 | 聊天继续 |
| Gateway outbox DB error | 不阻塞 raw events/turn write；记录诊断 | 本轮 delivery 缺失 | 聊天继续，health degraded |
| Haven raw event failure | 沿用 Haven 现有错误政策；不得用 Xinchao 代替真源 | 不虚构已归档 | adapter 可独立降级 |

## 12. 诊断合同

默认 health/debug 可返回：

- adapter enabled/configured；
- reachable（最近一次探测/调用）；
- last context success time；
- last delivery success time；
- pending/failed 数；
- current request degraded；
- context estimated tokens、section count；
- sanitized error code。

默认不得返回：

- SERVICE_TOKEN；
- continuity 正文；
-完整 session/profile ID；
- payload_json；
-上游或内部秘密 URL。

只有 Haven 现有受保护 full debug 才能返回经过截断和再次清理的注入摘要。

## 13. 兼容性完成标准

- adapter disabled 与没有配置时，Gateway 行为逐项等同现状；
- OpenAI/Anthropic 流式和非流式均使用同一 adapter 边界；
- Xinchao `mode=turn` 不触发 Haven continuity；
- continuity HTTP 与 MCP 使用同一 store/function；
- outbox 重启恢复且相同事件不重复；
- Xinchao 断开不拖垮聊天；
- 当前消息、Just Now、Xinchao、Haven recall/raw evidence 无三重重复。
