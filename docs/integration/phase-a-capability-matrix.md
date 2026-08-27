# 阶段 A 三方能力矩阵

更新时间：2026-08-26

处置枚举：

- `KEEP_HAVEN`：保留 Haven 现有实现和数据结构；
- `PORT_P0LUZ`：迁入 P0luz 锁定基线的新行为；
- `MERGE_BEHAVIOR`：保留 Haven 外形，吸收 P0luz 正确性修复；
- `USE_XINCHAO`：由 Xinchao 单独负责；
- `ADD_ADAPTER`：新增 Haven/Xinchao 兼容层；
- `DROP_DERIVED_DATA`：旧派生索引不迁移，重新生成；
- `OUT_OF_SCOPE`：本计划明确不处理。

## 1. 长期记忆核心

| 能力 | Haven 现状/真源 | P0luz 锁定基线行为 | Xinchao 角色 | 处置 | 实施批次 | 验收合同 |
|---|---|---|---|---|---|---|
| 普通 bucket Markdown | canonical，含 Haven metadata | 包结构和 metadata 边界更严 | 不保存副本 | `MERGE_BEHAVIOR` | B6 | 旧 bucket 无需全量重写即可读写 |
| hold | Haven 公共工具与媒体扩展 | domain override、source evidence、错误语义修复 | 仅代理 | `MERGE_BEHAVIOR` | B1/B4/B6 | 正文成功即成功；错误脱敏；旧参数兼容 |
| grow | Haven 写入链路 | items、test_data、断连/超时幂等、source_ranges | 不保存副本 | `MERGE_BEHAVIOR` | B3/B6 | 相同请求只产生一次结果；异步 embedding 不回滚正文 |
| trace | Haven 含 media/meaning 兼容 | content patch、显式 plan、归档恢复、错误合同 | 仅代理 | `MERGE_BEHAVIOR` | B1/B6 | noop/失败语义与 schema 明确；旧 bucket 可修改 |
| feel | Haven 感受层 | feel 边界、dream/breath 输出保护 | 动态状态不等于 feel | `MERGE_BEHAVIOR` | B6 | feel 不成为普通事实查询的直接 seed |
| I 候选/沉淀 | Haven 长期自我认知 | dream witness、pending candidate 修复 | 动态心智不持久复制 | `MERGE_BEHAVIOR` | B6 | 候选升级规则不被 Xinchao 状态替代 |
| plan | Haven 长期待办 | 显式 resolution、禁止误自动完成 | handoff 不是 plan | `MERGE_BEHAVIOR` | B6 | plan 状态仅显式或受控流程改变 |
| letter | Haven 永久信件 | lock、历史迁移、返回格式修复 | 不保存信件副本 | `MERGE_BEHAVIOR` | B6 | 锁定 letter 不从其他记忆表面泄漏或被 AI 改写 |
| anchor/catalog | Haven 公共工具 | pinned 优先预算、catalog/anchor 文案与边界 | 无 | `MERGE_BEHAVIOR` | B6 | anchor 数量、旧 metadata、catalog 目录行为保持 |
| archive/delete/test_data | Haven 归档和受控清理 | human deletion、test_data hard delete、restore touch | 无 | `MERGE_BEHAVIOR` | B6 | 普通数据不可 hard-delete；测试数据可验证清理 |

## 2. Source、Relation 与证据

| 能力 | Haven 现状/真源 | P0luz 行为 | 处置 | 实施批次 | 不变量 |
|---|---|---|---|---|---|
| Source 原文证据 | Haven 有 `source_refs.py` 与 source metadata | 2.17.7 可逆 `source_links`、稳定 slot、shared immutable blob | `MERGE_BEHAVIOR` | B4 | Source 不改正文、生命周期、recency 或 embedding |
| Source attach/read/detach/restore | Haven 外形优先 | 多 Source manifest、显式 slot/all_sources、幂等恢复 | `PORT_P0LUZ` | B4 | detached 保留 slot；归档桶不会因 restore 复活 |
| Source 备份闭包 | Haven backup/import 保留 | active + detached 取并集 | `PORT_P0LUZ` | B7 | 不丢失 detached evidence；不重复复制 blob |
| Relation 存储 | Haven 有图/edge 能力，但非 P0luz ledger 合同 | 2.17.10 ID-first、双向镜像 | `MERGE_BEHAVIOR` | B5 | 保留 Haven 图层；新增 ledger 不替代图数据库 |
| Relation 方向语义 | Haven 原能力保留 | caused_by↔causes、continuation_of↔continues、对称类型、custom | `PORT_P0LUZ` | B5 | 固定类型反向语义确定；custom 才允许 label |
| Relation detach/restore | Haven 行为需适配 | 有序双桶锁同步镜像；legacy 单向兼容 | `PORT_P0LUZ` | B5 | 两端共享 relation_id；legacy 不强迁移 |
| Relation 召回 hint | Haven recall 仍掌控候选 | 最多两条、非候选、非递归 | `MERGE_BEHAVIOR` | B5 | Relation 不独立候选、不 embedding、不递归扩散 |
| Source/Relation migration remap | Haven import 是主入口 | keep-both ID 重写与目标缺失 fail-closed | `PORT_P0LUZ` | B7 | 不误连本地同 ID；缺失目标原位 detached |

## 3. 检索、索引与派生数据

| 能力 | 处置 | 所有者 | 实施决定 | 验收 |
|---|---|---|---|---|
| Embedding 主索引 | `MERGE_BEHAVIOR` | Haven | 保留当前模型/配置；迁入 P0luz outbox、状态与恢复语义 | Markdown 已写入时 embedding 失败不回滚；可补排队 |
| P0luz embedding outbox | `PORT_P0LUZ` | Haven | 适配 Haven state_dir 和现有 engine | 重启恢复、去重、失败计数测试通过 |
| Supabase 旧向量 | `DROP_DERIVED_DATA` | 无 | 不读取、不导入、不删除源 | 新索引中不存在旧 vector/model 字段 |
| 迁移摘要 embedding | `DROP_DERIVED_DATA` | Haven | 最终 Markdown 写完后统一重建 | active 可召回摘要覆盖率符合报告 |
| Raw event embedding | `OUT_OF_SCOPE`（明确禁止） | 无 | 永不创建 | 普通 breath 无 raw event 候选 |
| BM25/词法召回 | `KEEP_HAVEN` | Haven | 保留 Haven 与 raw FTS 职责分离 | 普通 bucket 与 raw archive 查询路径可区分 |
| 召回门控/去重 | `MERGE_BEHAVIOR` | Haven Gateway/Brain | 保留 Haven 多层策略；吸收经合同验证的 P0luz正确性修复 | 当前请求、Just Now、Xinchao、Haven 只保留最高层副本 |
| 图关系/Word Map | `KEEP_HAVEN` | Haven | 不被 Relation ledger 覆盖 | 图提示保持低优先级且非递归污染 |
| affect/feel 召回 | `KEEP_HAVEN` | Haven | 情感仅作相关性辅助，不作事实 seed | 普通事实查询不被泛情绪摘要占据 |

## 4. Haven 专属能力

以下全部是不可破坏合同，P0luz 文件不得覆盖删除。

| 能力 | 处置 | 说明 |
|---|---|---|
| Gateway OpenAI `/v1/chat/completions` | `KEEP_HAVEN` | 公共聊天 API、流式/非流式、上游路由 |
| Gateway Anthropic `/v1/messages` | `KEEP_HAVEN` | 保持 tool/reasoning/cache 兼容 |
| Prompt Cache | `KEEP_HAVEN` | OpenAI key 与 Anthropic cache control |
| Gateway `conversation_turns` | `KEEP_HAVEN` | 同 session 即时指代与 Xinchao 故障 fallback |
| raw events | `KEEP_HAVEN` | 原始 user/assistant 冷档案、FTS/日期/原句 |
| Persona | `KEEP_HAVEN` | 低权重语气/关系状态，不交给 P0luz |
| Portrait | `KEEP_HAVEN` | 画像证据和卡片生命周期 |
| Daily Reflection/Dream | `KEEP_HAVEN` | Haven 长期反思体系；与 Xinchao 动态梦境区分 |
| Darkroom | `KEEP_HAVEN` | 房间与删除能力 |
| reminder | `KEEP_HAVEN` | 照顾备忘与提醒存储 |
| favorite/comment/profile_fact | `KEEP_HAVEN` | Haven 社交与事实辅助能力 |
| moment/node/edge | `KEEP_HAVEN` | 长期记忆结构与图层 |
| Dashboard/诊断 | `MERGE_BEHAVIOR` | 保留 Haven UI，增加 adapter 非敏感诊断 |
| 媒体存储/下载 | `KEEP_HAVEN` | 未来图片持久化、鉴权、路径安全、MIME |

## 5. Xinchao 与 Gateway

| 能力 | 处置 | 唯一所有者 | 合同 |
|---|---|---|---|
| 动态心智/drive state | `USE_XINCHAO` | Xinchao | Gateway 只读低权重状态，不复制状态机 |
| recent continuity | `USE_XINCHAO` | Xinchao | profile 内跨 session；`turn_id` 幂等 |
| handoff note | `USE_XINCHAO` | Xinchao | 有界、短期、非长期记忆 |
| MCP/OAuth 公共入口 | `USE_XINCHAO` | Xinchao | 手机/桌面 MCP 连接 Xinchao，代理 Haven 工具 |
| Gateway session mapping | `ADD_ADAPTER` | Haven Gateway | `gateway:<client_label>:<session>`，超长稳定哈希 |
| Continuity HTTP write | `ADD_ADAPTER` | Xinchao | `POST /v1/continuity/sync`，复用 MCP 内部函数 |
| Context HTTP read | `ADD_ADAPTER` | Haven Gateway | `GET /v1/context?...mode=turn`，默认 600 tokens |
| Conversation event write | `ADD_ADAPTER` | Haven Gateway → Xinchao | 成功完整轮次一次；默认不猜 interaction_type |
| Delivery outbox | `ADD_ADAPTER` | Haven Gateway | SQLite、幂等、指数退避、最大保留期 |
| Xinchao 故障降级 | `ADD_ADAPTER` | Haven Gateway | 不返回 5xx；回退同 session conversation_turns |
| OB continuity in Gateway Xinchao call | `OUT_OF_SCOPE`（禁止） | 无 | `mode=turn` 不调用 OB，避免双重注入 |
| MCP-over-MCP Gateway adapter | `OUT_OF_SCOPE` | 无 | 私有 HTTP，不在请求链路建 MCP 会话 |

## 6. Supabase 与媒体

| 能力 | 处置 | 决定 |
|---|---|---|
| Supabase 11,919 原文 | `KEEP_HAVEN` | 离线批量转入 `raw_events.sqlite`，无向量 |
| Supabase 842 摘要正文 | `MERGE_BEHAVIOR` | 去重、证据约束重写、合并/拒绝后生成 bucket |
| Supabase 旧 embedding | `DROP_DERIVED_DATA` | 全部废弃，不进入新索引 |
| 旧 128 冷档案 bucket | `OUT_OF_SCOPE` | 不 apply；只保留离线审计包 |
| 历史媒体 | `OUT_OF_SCOPE` | 不迁移 |
| 未来图片 | `KEEP_HAVEN` | 随所属文字记忆保存和返回；不做 CLIP/图搜图 |

## 7. 无悬空结论

- Haven 是唯一长期记忆和 raw archive 真源。
- Xinchao 是唯一动态状态、短期 continuity、handoff 与公共 MCP/OAuth 所有者。
- Gateway 是 Haven 公共聊天层，必须保留；只通过私有 HTTP adapter 访问 Xinchao。
- Embedding 是可重建派生索引；Supabase 旧向量和 raw events 不进入它。
- P0luz 迁移范围锁定为 `7c88175 / 2.17.11`；基线后 67 个提交不自动加入。
