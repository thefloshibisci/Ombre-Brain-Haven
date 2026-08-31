# 摘要直入导入（Summary-only Import）

更新时间：2026-08-30

## 决策变更

E2 原方案要求逐条人工审核 842 条 legacy summary、绑定 raw event 证据后才允许导入。实际审核第 1 条即确认成本不可接受（842 条 × 逐条读原文核对）。

改为**摘要直入**：842 条摘要按原文导入为记忆桶，不做人工审核、不绑定 raw 证据。

原因与代价：

- 摘要本身质量可用：842 条全部非空，长度中位数 61 字、最长 149 字，无一条完全重复（归一化 SHA-256 无碰撞）；
- raw archive（11,919 条）保持只读冷档，**不 embedding、不进 breath**，与原目标一致；
- 放弃的是"每条摘要可回溯到具体对话轮次"这一强证据链。摘要 `created_at` 是 6 小时 cron 落库时刻而非对话时间，本身已不精确；
- `stage-e-summary-review-working.jsonl` 与审核工具链保留但停用，不删除，便于日后需要时恢复逐条审核。

## 实现

`D:\silence\Ombre-Brain\scripts\import_summary_memories.py`，三个子命令：`plan`（离线）、`apply`（写入，可续跑）、`verify`（抽样回读）。

写入通道是 `POST /api/memories`（`server.py:9904`），用 `OMBRE_MEMORY_WRITE_TOKEN` 鉴权。该端点按 `id` 幂等 upsert，并自动排队 embedding 与 enrichment，因此不需要新增任何运行时代码。

### 字段映射

| 桶字段 | 来源 |
| --- | --- |
| `id` | `legacy-<legacy_summary_id>`，稳定且可重入 |
| `content` | `legacy_content` 原文逐字保留，不改写 |
| `title` | 从正文首个子句截取，14–34 字 |
| `type` | 固定 `dynamic` |
| `domain` | 关键词打分，最多 2 个 canonical 域 |
| `tags` | `legacy_summary` + 条件标签 |
| `importance` | `candidate` 6，`backlog` 5 |
| `created` / `last_active` / `updated_at` | `created_at` 转 +08:00 |

`domain` 只使用 `memory_metadata.DOMAIN_LABELS` 的 7 个 canonical 键（`relationship / intimacy / inner / life / tech / project / general`）。注意 `scripts/reclassify_domains.py` 里那张 22 域中文关键词表早于当前主域表，**不要**再用它做分类。

### 标签语义

- `legacy_summary`：全部 842 条，标记来源，便于日后整体筛出或回滚；
- `曾用名`：275 条正文出现"陆沉"。这批档案横跨改名，正文一律不改写，靠标签让改名可被检索到；
- `时间存疑`：14 条落在 `initial_backfill` 窗口，其 `created_at` 是首次回填批次时间，与对话时间可能相差很远。

## 执行

生成计划（纯离线，不联网、不写任何服务）：

```powershell
python scripts/import_summary_memories.py plan `
  --artifact D:\silence\backups\stage-e-summary-artifact.json `
  --windows D:\silence\backups\stage-e-summary-evidence-windows.json `
  --output D:\silence\backups\stage-e-summary-import-plan.json
```

计划基线：842 行全部入选、`ok=true`、`errors=[]`、无重复正文；域分布 `relationship 269 / tech 204 / life 169 / inner 157 / general 150 / intimacy 133 / project 126`；时间范围 `2026-07-24T23:18:53+08:00` → `2026-08-19T20:00:24+08:00`。

先 dry-run 看 payload 形态，再实际写入。`apply` 按 `--state` 续跑，已成功的行不会重发：

```powershell
$env:OMBRE_MEMORY_WRITE_TOKEN = "<staging write token>"
python scripts/import_summary_memories.py apply `
  --plan D:\silence\backups\stage-e-summary-import-plan.json `
  --base-url https://ombre-staging-6087df.zeabur.app `
  --state D:\silence\backups\stage-e-summary-import-state.jsonl `
  --report D:\silence\backups\stage-e-summary-import-report.json `
  --sleep 1.0
```

安全阀：HTTP 5xx 与传输错误按指数退避重试（默认 3 次），4xx 不重试；连续 5 条失败即中止，避免在服务异常时刷满一整批。

## 抽样验证

读端点（`/api/bucket/{id}`、`/api/buckets/light`）不接受写 token，均返回 401，`POST /api/memories` 是该 token 唯一可用通道。因此 `verify` 重发完全相同的 payload 并检查两件事：

- `status=updated` — 证明该 bucket id 已存在，写入确实落库；
- `embedding=skipped` — 证明服务端 content hash 未变，即库里正文仍等于导入的正文。

```powershell
python scripts/import_summary_memories.py verify `
  --plan D:\silence\backups\stage-e-summary-import-plan.json `
  --base-url https://ombre-staging-6087df.zeabur.app `
  --state D:\silence\backups\stage-e-summary-import-state.jsonl `
  --sample 20
```

`--state` 会把抽样范围限定在已导入的行，避免把未导入的行误报为失败。

## 增量导入（新增摘要）

`stage-e-summary-artifact.json` 来自 2026-08-20 的一次性导出（842 行，SHA-256 `417ecfdf…`，见 `baseline-evidence.md`）。旧系统的 6 小时 cron 仍在继续产出摘要，因此 legacy 侧总量会持续增长（2026-08-31 已达约 991 条）。

工具本身不需要改动即可处理增量：`bucket id = legacy-<legacy_summary_id>` 与行号无关，`--state` 按 bucket id 去重。因此重新导出一份**全量超集**、重新 plan、再对着**同一个 state 文件** apply 即可：已导入的行会被跳过，只有新行会发请求。

### 第 1 步：从 Supabase 重新导出

导出必须还原成原始的容器 CSV 形状，否则 `read_container_csv` 会直接报 `unexpected container columns`：外层严格三列 `export_date, record_count, records`；`records` 是一个 JSON 数组；内层每条对象为 `id, content, created_at, reviewed_at, assistant_id, review_status`，**不含 embedding 向量**。

已核对旧 CSV 的分组规则：`export_date` 等于该组 `created_at` 的 UTC 日期，`record_count` 等于组内条数。对应 SQL（Supabase Dashboard → SQL Editor 执行，再用结果面板的 Download CSV 下载）：

```sql
select
  to_char(created_at at time zone 'UTC', 'YYYY-MM-DD') as export_date,
  count(*)                                             as record_count,
  json_agg(
    json_build_object(
      'id',            id,
      'content',       content,
      'created_at',    created_at,
      'reviewed_at',   reviewed_at,
      'assistant_id',  assistant_id,
      'review_status', review_status
    )
    order by created_at
  ) as records
from <摘要表>
group by 1
order by 1;
```

`<摘要表>` 取 Table Editor 里字段为 `id / content / created_at / reviewed_at / assistant_id / review_status` 的那张表。不要加 `where`：导出全量超集，去重交给 state 文件，比按时间切片更不容易漏行。

下载后放进 `D:\silence\backups\`，命名沿用 `supabase-memory-summaries-no-embedding-<YYYYMMDD-HHMMSS>.csv`。

### created_at 为空的行

2026-08-31 的 990 行导出里有 1 行 `created_at` 是 NULL（`legacy-6c6cbed9-2318-44a4-98f6-327ea9e3732e`，正文完整、`review_status=candidate`）。`reviewed_at`、`event_time_start`、`event_time_end` 同为空，且 Table Editor 导出按 `id` 排序，邻居行的时间给不出任何线索，源库里没有可推导的时间。

`plan` 默认仍然**拒绝**这种行（记入 `errors`、不导入），避免静默编造时间。需要导入时必须显式给一个替代时间：

```powershell
python scripts/import_summary_memories.py plan `
  --artifact ... --windows ... `
  --missing-created-at "2026-08-26T12:00:47.685953+00:00" `
  --output ...
```

该值只作用于 `created_at` 为空的行，有时间的行完全不受影响。命中的行会同时打上 `时间存疑` 和 `时间缺失` 两个标签，并在 plan 里带 `source_time_missing=true`、`stats.missing_time_rows` 计数、`source.missing_created_at` 记录所用值，因此"这个时间是补的"永远可被检索和复核。

本轮取值 `2026-08-26T12:00:47.685953+00:00`（落库为 `2026-08-26T20:00:47+08:00`）来自严槿本人对该事件时间的定位，不是程序推断。

## 导出形状转换（Table Editor 行级导出 → 容器 CSV）

Supabase Table Editor 的整表导出是**行级 12 列**（`id, assistant_id, content, created_at, embedding, review_status, reviewed_at, promoted_at, ombre_bucket_id, review_note, event_time_start, event_time_end`），且 `embedding` 带 1024 维向量（990 行约 12.9 MB）。这与 `read_container_csv` 要求的三列容器形状不同，不能直接喂给 `migrate_supabase_archive.py summaries`。

转换规则（已用生产解析器回读验证）：按 `created_at` 的 **UTC 日期**分组，`export_date` 取该日期，`record_count` 取组内条数，`records` 是仅含 6 个内层字段的 JSON 数组，**丢弃 embedding**。`created_at` 为空的行归入 `export_date=unknown` 组。990 行 → 36 组，回读 990 条、零 warning，产物从 12.9 MB 降到 394 KB。

表名确认为 `memory_summaries`，`assistant_id` 单一值 `0950e2dc-9bd5-4801-afa3-aa887aa36b4e`。

### 第 2 步：重新生成 artifact 并 plan

```powershell
python scripts/migrate_supabase_archive.py summaries `
  --summary-csv D:\silence\backups\supabase-memory-summaries-no-embedding-<新时间戳>.csv `
  --report D:\silence\backups\stage-e-summary-artifact-<新时间戳>.json

python scripts/import_summary_memories.py plan `
  --artifact D:\silence\backups\stage-e-summary-artifact-<新时间戳>.json `
  --windows D:\silence\backups\stage-e-summary-evidence-windows.json `
  --output D:\silence\backups\stage-e-summary-import-plan-<新时间戳>.json
```

放行条件：`ok=true`、`errors=[]`、`artifact_rows` 不小于旧的 842（小于说明导出漏行，不要继续）。旧 artifact 与旧 plan 保留不覆盖，便于对比。

### 第 3 步：对着同一个 state 文件 apply

```powershell
$env:OMBRE_MEMORY_WRITE_TOKEN = "<staging write token>"
python scripts/import_summary_memories.py apply `
  --plan D:\silence\backups\stage-e-summary-import-plan-<新时间戳>.json `
  --base-url https://ombre-staging-6087df.zeabur.app `
  --state D:\silence\backups\stage-e-summary-import-state.jsonl `
  --report D:\silence\backups\stage-e-summary-import-report-<新时间戳>.json `
  --sleep 1.0
```

**必须复用 `stage-e-summary-import-state.jsonl`**；换新 state 文件会把 842 条已导入的行整批重发。

`test_growing_plan_only_writes_the_new_rows` 与 `test_bucket_ids_are_stable_across_replanning` 两条测试锁定了这个行为。

## 测试

`D:\silence\Ombre-Brain\tests\test_import_summary_memories.py`（31 条），覆盖：域键必属 canonical 集合、并列时更specific 的域优先、标题吸收子句到可读长度且不超上限、UTC→+08:00 转换、计划可重复生成且逐字保留正文、重复 legacy id 报错而不导入、改名标签与时间存疑标签、payload 字段集合与 API 契约一致、`ok=false` 拒绝 apply、dry-run 不写、续跑跳过已成功行、失败行会重试、4xx 不重试、连续失败中止、`--limit` 生效。