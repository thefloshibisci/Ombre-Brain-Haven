# 不可破坏合同与回归门禁

更新时间：2026-08-26

本文是阶段 B–F 的停止线。任一 `P0` 合同失败时不得继续正式迁移或生产替换评估。

## 1. 优先级

- `P0`：事实真源、数据完整性、鉴权、Gateway 可用性或生产隔离；失败立即停止。
- `P1`：兼容性、幂等、召回质量与诊断；必须在阶段完成前关闭。
- `P2`：Dashboard 展示、文案和非关键可观测性；可在同阶段末修复，但不能隐瞒。

## 2. Canonical 与数据真源

| ID | 优先级 | 合同 | 证据/测试 |
|---|---|---|---|
| NR-001 | P0 | Haven buckets 是唯一长期记忆真源 | Xinchao state 不出现 bucket 正文副本；架构扫描 |
| NR-002 | P0 | `raw_events.sqlite` 是原始对话冷档案，不是普通 bucket | 普通 `breath` 查询不返回 raw event；日期/原句专用查询可返回 |
| NR-003 | P0 | Embedding 是可重建派生索引 | 删除 staging index 后可从 active bucket 重建；Markdown 不变 |
| NR-004 | P0 | Supabase 在迁移全程只读且不删除 | 迁移脚本只接受文件输入；无 Supabase 写 SDK/SQL |
| NR-005 | P0 | Xinchao 不成为长期记忆第二真源 | continuity/context payload 有界且 TTL/store 类型明确 |
| NR-006 | P0 | 旧 128 冷档案 bucket 包不 apply | 脚本和文档中无 apply 路径；staging bucket inventory 不出现这些 ID |

## 3. Haven 专属功能保留

以下文件/路由/能力在每批 P0luz 迁移后必须仍存在：

- `gateway.py`、`gateway_state.py`；
- `/v1/chat/completions`、`/v1/messages`；
- OpenAI/Anthropic streaming 与非 streaming；
- Prompt Cache；
- `raw_events.py` 与 raw query/ingest 受保护路由；
- Persona、Portrait、Daily Reflection、Dream；
- Darkroom、reminder、favorite/comment/profile_fact；
- moment/node/edge、Word Map、图扩散；
- media store/download；
- Dashboard、storage diagnostics、safe migration bridge。

测试门：

| ID | 优先级 | 合同 |
|---|---|---|
| NR-010 | P0 | 不允许用 P0luz `server.py`、`bucket_manager.py` 或部署目录整文件覆盖 Haven |
| NR-011 | P0 | adapter disabled 时 Gateway 输出与现有行为兼容 |
| NR-012 | P0 | 客户端模型名与上游路由选择不被 adapter 改写 |
| NR-013 | P1 | Prompt Cache 稳定区不因 Xinchao 动态上下文每轮失效 |
| NR-014 | P1 | Haven protected diagnostics 不泄露正文、token、payload_json |
| NR-015 | P1 | Haven 旧 bucket、archive、feel、plan、letter 不需一次性 metadata 迁移 |

## 4. P0luz 行为移植门禁

### B1 错误和校验

- 所有公共异常执行 credential/token/key/Authorization/URL 脱敏；
- 最大公开长度固定；
- 不以“API key 肯定没问题/一定有问题”等不可证实文案代替真实错误；
- 内部日志也不得记录 secret；可记录短 error code。

### B2 Embedding outbox

- Markdown/bucket 成功落盘后，即使 embedding provider 失败也返回正文写入成功；
- 相同 bucket/model 只保留一个待处理项；
- worker 重启可恢复；
- failed 可重新 enqueue；
- 归档/删除/duplicate staging item 不进入 active index；
- raw events 永不 enqueue。

### B3 Grow 幂等

- 相同 idempotency identity 的并发/超时重试复用任务；
- 客户端断连不产生第二批 bucket；
- 不以纯正文相似作为幂等键；
- failed-before-write 可安全重试；
- completed result 可有界复用并过期清理。

### B4 Source

- source blob 共享、不可变；
- slot 稳定且 detach 不压缩；
- source_refs 继续是 active compatibility projection；
- Source 不改变 bucket body、importance、recency、activation、embedding；
- 归档 bucket 可管理 source，但不会被 restore source 复活；
- locked letter 边界不被 source API 绕过。

### B5 Relation

- 新 relation 有唯一 `relation_id` 和双向镜像；
- 固定类型的反向语义确定；
- legacy 单向记录原位可读；
- detach/restore 两端原子一致；
- Relation 不独立候选、不 embedding、不 activation、不 decay、不递归遍历；
- keep-both 目标缺失时原位 detached，不误连本地同 ID。

### B6/B7 工具、备份和迁移

- plan 不被模糊推断自动完成；
- letter lock 在 breath/dream/search/dashboard/import 全表面生效；
- test_data hard-delete 只适用于创建时明确标记的测试桶；
- import staging 失败后原仓不变；
- backup 闭包包含 active/detached Source/Relation；
- Claude conversation 识别不会把普通 JSON 误判；
- keep-both 重写所有包内引用。

## 5. Gateway/Xinchao 合同测试

| ID | 优先级 | 场景 | 预期 |
|---|---|---|---|
| GX-001 | P0 | adapter 未配置/disabled | Gateway 与旧行为一致，不访问 Xinchao |
| GX-002 | P0 | `GET context` timeout | 1.5s 左右降级，同 session local turns 继续，聊天非 5xx |
| GX-003 | P0 | Xinchao 401 | 聊天继续；diagnostic auth degraded；不打印 token |
| GX-004 | P0 | Xinchao 返回其他 profile/非法 section | fail closed，不注入 |
| GX-005 | P0 | 请求指定 `mode=session_start` 的代码路径 | 静态/单测禁止，Gateway 固定 `mode=turn` |
| GX-006 | P0 | 完整非流式 assistant | raw events + turns + 两类 outbox 顺序成立 |
| GX-007 | P0 | 完整流正常结束 | 只记录最终可见 assistant 文本 |
| GX-008 | P0 | 流中断/上游异常 | 不写 assistant continuity，不写半截 raw event |
| GX-009 | P1 | worker 重启 | pending 补送，Xinchao accepted 或 duplicate 后完成 |
| GX-010 | P1 | 相同 event 重投 | 动态状态和 continuity 都不重复增长 |
| GX-011 | P1 | 当前消息同时出现在 Xinchao | 只保留当前请求层副本 |
| GX-012 | P1 | prompt cache | 稳定 system/tools cache 行为不退化 |
| GX-013 | P1 | 同 session “刚才” | local `conversation_turns` 优先恢复 |
| GX-014 | P1 | 跨 session | Xinchao 返回同 profile 其他 session bounded continuity |
| GX-015 | P1 | Xinchao unavailable | 仅在此时允许 persona 范围的有限 local cross-session fallback |
| GX-016 | P1 | tool call/reasoning | 不进入 raw events 或 continuity |
| GX-017 | P1 | OpenAI/Anthropic 四模式 | 两协议流式/非流式共享 adapter 语义 |

## 6. Supabase 迁移门禁

### Inventory

- 输入 hash 与 `baseline-evidence.md` 一致；
- raw 11,919、summary 842、conversation 25；
- 统计 user/assistant/invalid roles；
- 统计空正文、重复 ID、正文 hash 重复、时间范围；
- 报告不得包含完整聊天内容。

### Raw apply

- 单批 ≤ 1,000；
- 只调用 `RawEventStore.ingest()`；
- `(source, source_event_id)` 和 event hash 双重幂等；
- 无 embedding、无 bucket、无 MCP；
- 合法数满足 `inserted + duplicate`；
- FTS5 可用时日期/原句测试命中；不可用时明确报告 fallback，而非伪称通过。

### Summary rewrite

- 842 个 legacy ID 每个有且仅有 keep/rewrite/merge/reject；
- 不导入 vector 字段；
- rewrite 只引用 legacy content + 已关联 raw events；
- `original` 均可回查；
- 不因向量相似合并不同日期/人物/事件；
- migration provenance 不进入 embedding 文本；
- final active bucket 才 enqueue embedding。

### Recall noise

必须用固定 query set 比较迁移前/后 staging：

- 事实；
- 偏好；
- 项目；
- 感受；
- 指定日期；
- 原句。

通过条件：

- 普通事实/偏好/项目查询前列不被 raw events 占据；
- affect-only/generic summary 的前列占比下降；
- 日期/原句通过 raw archive 专用路径可达；
- 无旧向量模型标识或向量 payload。

## 7. 媒体合同

| ID | 优先级 | 合同 |
|---|---|---|
| MD-001 | P0 | 不迁移历史媒体 |
| MD-002 | P0 | 新图片只能落入配置的 Haven persistent media 根目录 |
| MD-003 | P0 | 下载/读取防路径穿越并继续鉴权 |
| MD-004 | P1 | MIME 与文件扩展/检测结果一致 |
| MD-005 | P1 | bucket 只存受控 metadata 和相对引用，不存任意本机绝对路径 |
| MD-006 | P1 | 文字记忆命中时返回 media metadata/可访问引用 |
| MD-007 | P1 | 图片不独立产生召回候选，不创建 CLIP/多模态向量 |
| MD-008 | P1 | trace replace/append 不可借路径删除 media 根目录外文件 |

## 8. 隔离与生产门禁

| ID | 优先级 | 合同 |
|---|---|---|
| ISO-001 | P0 | 实验服务、token、URL、卷与生产完全分离 |
| ISO-002 | P0 | compose/env/config 扫描不出现已知生产内部 URL/卷名 |
| ISO-003 | P0 | 测试脚本默认只绑定 localhost 或显式 staging target |
| ISO-004 | P0 | 本计划不执行 stop/redeploy/replace/migrate production |
| ISO-005 | P0 | 最终只生成替换建议和回滚包，不自动切换 |
| ISO-006 | P1 | adapter token 来自环境变量且 health 不回显 |
| ISO-007 | P1 | staging 数据卷有快照/hash/恢复说明 |

## 9. 最终一次全量测试顺序

在各阶段定向测试通过后，最终只执行一次完整验证：

1. Haven unit/integration；
2. Xinchao continuity/context/MCP/OAuth；
3. Gateway OpenAI/Anthropic；
4. migration/raw events/embedding；
5. media persist/return/security；
6. isolated topology/production-address scan。

最终产物：

- `acceptance-report.md`；
- `difference-report.md`；
- `migration-report.json` 与人类可读摘要；
- `rollback/` 配置、schema、volume snapshot 清单；
- `production-replacement-recommendation.md`。

上述产物齐全且所有 P0/P1 关闭前，结论只能是“不建议替换生产”。
