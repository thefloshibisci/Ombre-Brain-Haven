# 阶段 A 基线证据

更新时间：2026-08-26

本文只记录隔离整合工作的事实基线，不改变任何运行中服务、生产分支、生产卷或 Supabase 源数据。

## 1. 锁定仓库

| 角色 | 路径 | 分支 | 锁定提交 | 工作树 |
|---|---|---|---|---|
| Haven canonical | `D:\silence\Ombre-Brain` | `codex/miss-compat` | `ae1c4958e7920c61e0a0ad10afcac8650e663f8d` | clean |
| P0luz 更新参考 | `D:\silence\P0luz-Ombre-Brain` | `main` | `7c8817520781fe263b254bcd05910564a60ec61d` | clean；相对 `origin/main` behind 67 |
| Xinchao 实验参考 | `D:\silence\xinchao-nian` | `codex/vnext-experiment` | `58cecb51178020d8853c095b5d0e963af95dc471` | clean |

基线核对命令：

```powershell
git -C <repo> status --short --branch
git -C <repo> log -1 --oneline
```

## 2. Haven/P0luz 分叉证据

P0luz 历史已通过一次 `git fetch --unshallow --tags origin` 补全。不得重复将锁定分支快进到远端。

人工与 Git 历史核对得到最近共同提交：

```text
e9d61b5d9de8fb102beacdabf5a5b0f6957162fc
fix: 移除本地保底脱水的过时描述
```

两边在 2026-04-21 左右开始独立演化：

- Haven 首批分叉行为包含 Gateway persona state、streaming Gateway、tool-call passthrough、Supabase sync 和 Anthropic messages Gateway。
- P0luz 首批分叉行为包含 embedding 独立配置、检索/评分修复、环境变量与 Dashboard 整理，随后迁入包结构和存储层重构。
- 从共同点到锁定基线，Haven 约 671 个提交，P0luz 约 335 个提交；整文件覆盖会丢失双方大量独有行为。

因此本项目采用“行为 + 合同测试”迁移，不以 `server.py`、`bucket_manager.py` 或部署目录整文件替换。

## 3. P0luz 锁定范围

当前正式范围止于 P0luz `2.17.11 / 7c88175`。其中最后几个版本提供了本计划明确需要的合同：

| 版本 | 关键行为 |
|---|---|
| 2.17.7 | Source 可逆绑定、稳定 slot、active/detached 投影、备份闭包 |
| 2.17.8 | Relation V1、稳定 slot、detach/restore、迁移 keep-both 重写 |
| 2.17.9 | Claude conversations 导出识别修复 |
| 2.17.10 | Relation ID-first、双向镜像、方向语义、legacy 单向兼容 |
| 2.17.11 | Relation MCP schema enum 与公开说明补齐 |

锁定基线后的远端提交从 `10d8722` 到 `8203539`，包含 3.x 工具精简、quotes、You/them、检索阈值、迁移和错误语义等变更。它们属于 `POST_BASELINE_CANDIDATE`：

- 不自动纳入当前阶段 B；
- 不作为 Haven 现有公共工具的破坏性升级依据；
- 只有单独评审、补合同、获得范围确认后才能迁入。

## 4. Haven 现状证据

Haven 当前直接包含以下独有模块，必须保持：

- `gateway.py`、`gateway_state.py`；
- `raw_events.py`；
- `persona_engine.py`、`portrait_engine.py`、`reflection_engine.py`、`dream_engine.py`；
- `darkroom.py`、`reminder_store.py`、`favorite_tags.py`；
- `memory_moments.py`、`memory_nodes.py`、`memory_edges.py`、`word_map.py`；
- `media_store.py`、`source_refs.py`；
- OpenAI `/v1/chat/completions`、Anthropic `/v1/messages` 与 Prompt Cache 配置。

`raw_events.py` 已具备：

- 默认 `state/raw_events.sqlite`；
- `raw_events` 主表与 FTS5；
- `(source, source_event_id)` 唯一索引；
- `(source, event_hash)` 唯一约束；
- user/assistant 角色限制；
- 单批默认 1,000、上限 5,000；
- 对记忆注入块和客户端附加 context 的清理。

因此 Supabase 原文迁移必须复用 `RawEventStore.ingest()`，不得再构造冷档案 bucket。

## 5. Xinchao 现状证据

`D:\silence\xinchao-nian\xinchao\src\server.js` 已包含：

- `createContextEnvelope()`；
- `synchronizeRecentContinuity()`；
- `GET /v1/context`；
- `POST /v1/conversation-event`；
- MCP `xinchao_continuity_sync` 通过 handler 调用同一 continuity 函数。

关键边界：

- 只有 `mode=session_start` 才尝试读取 Ombre continuity；
- `mode=turn` 只组装动态状态、handoff 和 recent continuity，不再次读取 Haven；
- Gateway 必须固定调用 `mode=turn`，避免长期记忆双重注入。

当前缺口：

- 私有 HTTP `POST /v1/continuity/sync`；
- 该 HTTP 路由与 MCP 共用函数的合同测试；
- Haven Gateway 侧 Xinchao adapter 与 delivery outbox。

## 6. Supabase 不可变来源

### 原始对话

- 文件：`D:\silence\backups\supabase-chat-messages-20260820-012615.csv`
- 大小：7,127,113 bytes
- 记录数：11,919
- 唯一 ID：11,919
- conversation：25
- 时间范围：2026-07-23T18:44:58.049Z 至 2026-08-20T01:24:44Z
- SHA-256：`78a2b0fd8887ed7bd74f6ee3d4613a61e2cf5b533a80e4cb1656fbe4f34d8b1d`
- CSV 外层字段：`export_date, record_count, records`
- records 内字段：`id, role, content, created_at, assistant_id, conversation_id`

### 摘要

- 文件：`D:\silence\backups\supabase-memory-summaries-no-embedding-20260820-012524.csv`
- 大小：346,795 bytes
- 记录数：842（不是估算中的约 900）
- 唯一 ID：842
- 时间范围：2026-07-24T15:18:53.815294Z 至 2026-08-19T12:00:24.323204Z
- SHA-256：`417ecfdfb981370933dc8c2a2e7f11e7d7fccda2637ca1ff01a1acbd6bddf125`
- CSV 外层字段：`export_date, record_count, records`
- records 内字段：`id, content, created_at, reviewed_at, assistant_id, review_status`
- review 状态：candidate 103、backlog 739
- 旧 embedding 未进入导出包。

### 既有迁移包

`D:\silence\backups\supabase-migration-vnext-20260824\migration-manifest.json` 记录了先前生成的 128 个冷档案 bucket 与 842 个摘要 review bucket。该包只作为审计证据：

- 不重新导出 Supabase；
- 不再次 apply 旧 128-bucket 包；
- 新迁移直接从上述 CSV 转换为 `raw_events.sqlite` 和最终摘要 bucket；
- Supabase 源及旧表保持只读且不删除。

## 7. 隔离边界

禁止触碰：

- 生产服务 `ombre-brain`、`xinchao-nian-caric`；
- 生产 token、内部 URL 和数据卷；
- 生产 connector；
- Supabase 源表的写入、删除、向量更新。

所有代码修改只允许落在上述三个本地隔离分支；所有迁移输出必须写入新的 staging 目录或临时数据库，完整验收前不得替换生产。
