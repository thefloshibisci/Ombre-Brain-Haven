# Supabase → Haven 字段映射与迁移合同

更新时间：2026-08-26
状态：阶段 A 锁定；阶段 E 实现目标

## 1. 数据源

本计划只读取以下不可变导出：

| 类型 | 路径 | 记录数 | SHA-256 |
|---|---|---:|---|
| 原始对话 | `D:\silence\backups\supabase-chat-messages-20260820-012615.csv` | 11,919 | `78a2b0fd8887ed7bd74f6ee3d4613a61e2cf5b533a80e4cb1656fbe4f34d8b1d` |
| 记忆摘要 | `D:\silence\backups\supabase-memory-summaries-no-embedding-20260820-012524.csv` | 842 | `417ecfdfb981370933dc8c2a2e7f11e7d7fccda2637ca1ff01a1acbd6bddf125` |
| 旧迁移 manifest | `D:\silence\backups\supabase-migration-vnext-20260824\migration-manifest.json` | 审计材料 | 由 manifest 内部记录 |

禁止：重读线上 Supabase、修改源 CSV、导入旧 embedding、再次 apply 旧 128 个冷档案 bucket。

## 2. CSV 容器格式

两个 CSV 均按日期聚合，每行包含：

```text
export_date,record_count,records
```

`records` 是 JSON 数组。迁移器必须逐行解析并验证：

- `record_count == parsed records length`；
- 所有 JSON 项均为 object；
- 跨行 ID 去重；
- 统计结果与不可变 manifest 一致；
- 解析不因单条非法记录丢失整日其他合法记录。

## 3. 原始对话字段映射

源记录字段：

```text
id, role, content, created_at, assistant_id, conversation_id
```

目标为 `RawEventStore.ingest()` 接受的 event：

| Supabase | Haven raw event | 规则 |
|---|---|---|
| 固定值 | `source` | `supabase` |
| `id` | `source_event_id` | trim 后必须非空；稳定幂等主键 |
| `role` | `role` | 仅 `user`/`assistant`；其他拒绝 |
| `content` | `text` | 原始可见正文；由 RawEventStore 再执行注入块清理 |
| `created_at` | `created_at` | 保留原时间点并规范化 ISO 8601；不得改为迁移时间 |
| `conversation_id` | `conversation_id` | 缺失允许空字符串，但记入统计 |
| 可恢复客户端 session | `session_id` | 当前 CSV 无该字段，默认空；不得猜测 |
| 固定值 | `client` | `supabase`；除非离线证据能明确恢复真实客户端 |
| `assistant_id` | `metadata.assistant_id` | 审计字段，不进入正文/embedding |
| `export_date` | `metadata.export_date` | 审计字段 |
| 源文件名 | `metadata.source_file` | 只存 basename，不存秘密路径 |
| 迁移批次 ID | `metadata.migration_batch` | 固定 manifest 派生值 |
| 固定值 | `metadata.source_table` | `chat_messages` |

示例：

```json
{
  "source": "supabase",
  "source_event_id": "1ffc5fda-ebe2-470c-8bfa-6505d0cac371",
  "role": "user",
  "text": "你能调用自己的记忆库不",
  "created_at": "2026-07-23T18:44:58.049Z",
  "conversation_id": "787d9c82-654a-43ce-ba1b-c82add60301a",
  "session_id": "",
  "client": "supabase",
  "metadata": {
    "assistant_id": "0950e2dc-9bd5-4801-afa3-aa887aa36b4e",
    "export_date": "2026-07-23",
    "migration_batch": "supabase-v2-20260826",
    "source_file": "supabase-chat-messages-20260820-012615.csv",
    "source_table": "chat_messages"
  }
}
```

## 4. 原始对话过滤与拒绝

在进入 `RawEventStore.ingest()` 前做结构过滤，Store 再做正文清理和最终校验。

拒绝原因至少包括：

- `missing_id`；
- `invalid_role`；
- `empty_text`；
- `invalid_created_at`；
- `malformed_record`；
- `record_count_mismatch`（容器告警，合法项仍可处理）；
- `memory_injection_only`；
- `hidden_reasoning_or_tool_payload`；
- `oversized_after_policy`（若 Haven 现有上限拒绝）。

角色：

- `user`、`assistant`：允许；
- `system`、`tool`、`function`、`developer`：拒绝；
- 未知角色：拒绝，不映射成 assistant。

正文：

- 不进行摘要、翻译或语义改写；
- 统一换行与明显 NUL/control character；
- 识别并剥离 `Core Memory`、`Recent Context`、`Recalled Memory`、`Related Memory`、`Xinchao Recent Context` 等历史注入块；
- 若剥离后为空则拒绝；
- 不把 tool call、tool result、hidden reasoning 伪装成可见 assistant 文本。

## 5. 原始对话批处理

实现形态：离线 Python 脚本直接 import Haven `RawEventStore`，不走 MCP 或 HTTP。

建议 CLI：

```text
python scripts/migrate_supabase_archive.py inventory --chat-csv ... --summary-csv ... --output-dir ...
python scripts/migrate_supabase_archive.py sample --manifest ... --raw-db ... --limit-raw 50 --limit-summaries 20
python scripts/migrate_supabase_archive.py apply-raw --manifest ... --raw-db ... --batch-size 1000
python scripts/migrate_supabase_archive.py verify-raw --manifest ... --raw-db ...
```

合同：

- 默认 dry-run/inventory，不写目标；
- `apply-raw` 必须显式指定 staging `raw-db`；
- 单批最多 1,000；
- 先依赖 `(source, source_event_id)`，再依赖 Store 的 event hash；
- 每批写独立 checkpoint 和统计，但不修改源 manifest；
- 同 manifest 重跑只产生 duplicates，不重复插入；
- 不创建 embedding，不调用 bucket manager；
- 输出报告不包含完整聊天正文。

## 6. 摘要字段映射

源字段：

```text
id, content, created_at, reviewed_at, assistant_id, review_status
```

迁移中间记录：

| Supabase | 中间字段 | 规则 |
|---|---|---|
| `id` | `legacy_summary_id` | 必须非空 |
| `content` | `legacy_content` | 离线审计包逐字保留；不直接进入 embedding |
| `created_at` | `created_at` | 事件/摘要原时间 |
| `reviewed_at` | `reviewed_at` | 可空，仅审计 |
| `assistant_id` | `assistant_id` | 仅审计 |
| `review_status` | `legacy_review_status` | 当前为 candidate/backlog；不直接决定 active |
| SHA-256(content normalized) | `legacy_summary_hash` | 精确正文去重 |
| 关联原文 | `source_event_ids` | 只能来自确定性 conversation/time/evidence 归组 |
| 重写器版本 | `rewrite_version` | 例如 `supabase-summary-rewrite-v1` |
| 审核结果 | `decision` | `keep|rewrite|merge|reject` |
| 证据置信度 | `evidence_confidence` | `high|medium|low` |

## 7. 摘要归组与重写

由于摘要 CSV 没有 `conversation_id` 或直接 raw event ID，关联必须可审计：

1. 优先使用既有离线迁移包/queue 中已保存的明确引用；
2. 再用 `assistant_id + created_at` 的有界时间窗寻找候选 conversation；
3. 只有摘要中的主体、事件和候选原文同时匹配时，才保存 `source_event_ids`；
4. 模糊匹配不能把不同日期、人物或事件合并；
5. 无法唯一关联时保留为空，并将 `evidence_confidence=low`，不得伪造来源。

处理顺序：

1. ID 去重；
2. normalized content hash 去重；
3. evidence group；
4. 噪音分类；
5. 受证据约束的 rewrite/merge；
6. 最终 Markdown 渲染；
7. 写 bucket；
8. 最终 active bucket 才进入 embedding outbox。

噪音分类至少包括：

- template/generic；
- missing subject；
- missing event/time context；
- affect-only adjective；
- duplicated event wording；
- unsupported by linked raw events；
- empty/invalid。

重写约束：

- 输入只允许 legacy summary + 已关联 raw events；
- 不补来源没有的事实；
- 事实、原句、反思分段；
- `original` 只放可核对原句；
- 同一事件多摘要可合并；不同日期/人物/事件不可因向量相似而合并；
- 无原文但摘要自身完整，可保留并降低证据置信度；
- LLM 输出必须经过结构 validator，不合格转人工/规则 backlog，不静默写入。

## 8. 最终 bucket provenance

最终 Markdown metadata 至少包含：

```yaml
migration:
  source: supabase
  batch: supabase-v2-20260826
  legacy_summary_ids:
    - "..."
  legacy_summary_hashes:
    - "..."
  source_event_ids:
    - "..."
  rewrite_version: supabase-summary-rewrite-v1
  decision: rewrite
  evidence_confidence: high
  migrated_at: "..."
```

Embedding 正文排除：

- `migration` 整块；
- `source_event_ids`；
- legacy IDs/hash；
-媒体路径；
-审计说明。

旧摘要全文只在离线 review artifact 中保存，不重复塞入每个 bucket。

## 9. 三轮迁移门禁

### 小样本

- raw 50 条；
- summaries 20 条；
- 临时 raw DB 与临时 bucket 目录；
- 验证 duplicate/date/quote search、rewrite、bucket、embedding outbox。

### 正式 apply

- 使用固定输入 hash；
- raw 与 summary 各一次；
- 必须有显式 `--apply` 与空/已声明 staging 目标；
- checkpoint 表明完成后不得再次执行正文重写。

### 最终 verify

报告：

- source/input/valid/inserted/duplicate/rejected；
- 每种 rejection reason；
- conversation 与时间范围；
- manifest/hash；
- summary keep/rewrite/merge/reject；
- embedding eligible/enqueued/indexed/failed；
- 抽样 raw date/quote query 和普通 recall；
- 不输出完整私密正文。

## 10. 完成方程

原文：

```text
valid_source_rows = input_rows - structurally_rejected_rows
valid_source_rows = inserted_rows + duplicate_rows
```

摘要：

```text
842 = kept_legacy_items + rewritten_legacy_items + merged_legacy_items + rejected_legacy_items
```

其中 merged 统计按“被合并的 legacy item”计；最终 bucket 数不要求等于 842，但每个 legacy ID 必须有唯一 decision 和目标 bucket/rejection reason。
