# E2 摘要审核队列

更新时间：2026-08-30

## 目的

`stage-e-summary-artifact.json` 只保存了 842 条 Supabase legacy summary 的原摘要和迁移状态，不能直接写入 Haven。E2 先生成一个**本地隔离、默认未审阅、未绑定证据**的 JSONL 队列，供人工逐条决定 `keep`、`rewrite`、`merge` 或 `reject`；本工具不会写 staging bucket、不会调用 Gateway/MCP，也不会接触生产服务。

实现：`D:\silence\Ombre-Brain\scripts\prepare_summary_review_queue.py`

## 队列字段

每行一个 `legacy_summary_id`，包含：

- `legacy_summary_id`、`legacy_summary_hash`、`original_content`、`original_content_sha256`：原摘要身份和可回查正文；
- `created_at`、`legacy_review_status`：迁移源时间和旧状态；
- `decision`：初始为 `null`，最终只能是 `keep | rewrite | merge | reject`；
- `rewritten_content`：仅 `rewrite` 可填写；
- `merge_target_id`：仅 `merge` 可填写，不能自环或指向不存在的摘要；
- `source_event_ids`：人工确认的 raw event ID，初始为空；候选区只保存不含正文的排序提示，不视为证据绑定；
- `evidence_confidence`：`none | low | medium | high`；
- `reviewer`、`reviewed_at`、`validation`：审计与逐条校验字段。

## 只读生成

```powershell
python scripts/prepare_summary_review_queue.py build `
  --artifact D:\silence\backups\stage-e-summary-artifact.json `
  --raw-db D:\silence\backups\raw_events.sqlite `
  --output D:\silence\backups\stage-e-summary-review-queue.jsonl `
  --report D:\silence\backups\stage-e-summary-review-queue-report.json
```

当前基线：

- 842 行，842 个唯一 legacy summary ID；
- raw archive 可用事件 11,884 条；
- 842 行均保持 `decision=null`、`source_event_ids=[]`；
- 842 行均有候选提示，但共 10,104 个候选仅用于人工定位，不能自动绑定；
- 生成校验无错误。

校验：

```powershell
python scripts/prepare_summary_review_queue.py validate `
  --queue D:\silence\backups\stage-e-summary-review-queue.jsonl `
  --raw-db D:\silence\backups\raw_events.sqlite `
  --report D:\silence\backups\stage-e-summary-review-queue-validation.json
```

只有已填写 reviewer、reviewed_at，并满足相应 evidence/rewrite/merge 约束的逐条记录才会通过校验。队列通过校验不代表已经批准导入；实际 import 仍必须在单独的显式阶段实现并在 staging 上 dry-run 后执行。

## 人工审核工作流

基线队列由 `prepare_summary_review_queue.py build` 生成后视为只读，不要在原文件上编辑。先创建独立工作副本：

```powershell
python scripts/review_summary_queue.py init `
  --source D:\silence\backups\stage-e-summary-review-queue.jsonl `
  --output D:\silence\backups\stage-e-summary-review-working.jsonl
```

查看进度（命令会同时按当前 raw archive 校验工作副本）：

```powershell
python scripts/review_summary_queue.py status `
  --queue D:\silence\backups\stage-e-summary-review-working.jsonl `
  --raw-db D:\silence\backups\raw_events.sqlite
```

查看下一条未审核记录及候选 raw 正文。候选只是定位提示；只有人工读过正文并确认相关性后，才可以把 ID 作为 `source_event_ids` 写入：

```powershell
python scripts/review_summary_queue.py show `
  --queue D:\silence\backups\stage-e-summary-review-working.jsonl `
  --raw-db D:\silence\backups\raw_events.sqlite `
  --next-pending
```

记录一条审核决定。`set` 使用临时文件 + 原子替换，不会覆盖 baseline；若合同校验失败，也不会写入工作副本：

```powershell
python scripts/review_summary_queue.py set `
  --queue D:\silence\backups\stage-e-summary-review-working.jsonl `
  --raw-db D:\silence\backups\raw_events.sqlite `
  --id <legacy_summary_id> `
  --decision keep `
  --source-event-id <confirmed-raw-event-id> `
  --evidence-confidence high `
  --reviewer <reviewer-id> `
  --reviewed-at 2026-08-30T20:00:00+08:00
```

- `keep` / `rewrite` 必须至少绑定一个真实 raw event；`rewrite` 还必须给 `--rewritten-content`。
- `merge` 必须给另一个存在的 `--merge-target-id`；`reject` 和 `merge` 不会因为候选提示而自动绑定证据。
- `source-event-id` 可以重复传入，工具会去重；未知 ID、错误决策、错误时间或不匹配的字段会拒绝整次写入。
- 工具不会自动生成 `keep`、不会自动选择证据、不会写 bucket、不会调用 embedding/Gateway/MCP。

实现：`D:\silence\Ombre-Brain\scripts\review_summary_queue.py`；测试：`D:\silence\Ombre-Brain\tests\test_review_summary_queue.py`。
