# 遗留摘要证据窗口修正（E2）

本文记录 842 条 Supabase 遗留摘要在人工审核前必须先修正的两个数据事实，以及
对应的只读工具与门禁。原始归档 `raw_events.sqlite`、摘要 artifact 和基线队列
在整个过程中保持不变。

## 事实一：raw 归档含 2,974 条时区影子副本

Supabase 导出的 `chat_messages` 里，同一条消息被记录了两次：一次带亚秒精度的
真实 UTC 时间，一次把 Asia/Shanghai 的墙上时间当成 UTC 写入，两者相差正好
8 小时（容差 2 秒），会话、角色与正文完全一致。

- 11,884 条 raw 事件中，2,974 对构成影子副本，canonical 事件 8,910 条。
- 24 个会话中有 12 个受影响；`c13868e3` 一个会话就有 1,610 对。
- 2,973 对由亚秒精度判定 canonical，1 对两侧精度相同，回退到取较早时间。

这不是迁移引入的缺陷，源 CSV 自带（同一 `source_file`、同一 `export_date`），
因此归档保持只读，只额外产出一份可复核的映射：

```
python scripts/audit_raw_event_duplicates.py \
  --raw-db D:\silence\backups\raw_events.sqlite \
  --output D:\silence\backups\stage-e-raw-duplicate-map.json
```

产物 schema `ombre-raw-duplicate-map-v1`，含 `shadow_source_event_ids`、逐簇的
canonical/shadow 对照与判定规则。脚本另行报告 45 组无法配对的同文重复
（真实复读，不做去重），供人工判断。

## 事实二：摘要 `created_at` 是 cron 批次写入时间，不是对话时间

842 条摘要落在 96 个写入批次里，批次内 653 个间隔小于 1 秒，批次之间基本为
6 小时；每条摘要的 `created_at` 日期与 CSV 的 `export_date` 842/842 一致。也就是
说它记录的是定时任务落库的时刻，而摘要描述的对话发生在此之前。

基线队列用 `created_at` ±36 小时的对称窗口取候选，因此：

- 10,104 条候选提示中 5,413 条（53.6%）在时间上不可能，发生在摘要写入之后；
- 489 行的首选候选无效；
- 2,461 条候选指向影子副本，411 行的候选列表归一化后会塌缩。

修正做法是按批次重建因果窗口：

```
python scripts/rebuild_summary_evidence_windows.py \
  --artifact D:\silence\backups\stage-e-summary-artifact.json \
  --raw-db D:\silence\backups\raw_events.sqlite \
  --duplicate-map D:\silence\backups\stage-e-raw-duplicate-map.json \
  --output D:\silence\backups\stage-e-summary-evidence-windows.json
```

窗口规则：

- 只保留 canonical 事件，影子副本整体剔除；
- 窗口上界是摘要写入时间，候选一律早于摘要，不存在未来事件；
- 窗口下界是上一个批次的写入时间（`cron_interval`，89 个批次）；
- 首个批次是历史回填，下界取归档最早事件（`initial_backfill`，1 个批次）；
- cron 中断导致间隔超过 12 小时的，下界收敛为 12 小时（`clamped_gap`，6 个批次）。

产物 schema `ombre-summary-evidence-window-v1`。结果：842 条摘要中 528 条的窗口
只覆盖单一会话，627 条的前 5 个候选同属一个会话；121 条没有词面重叠候选，27 条
窗口内没有任何事件，这些必须人工判断，不得凭猜测绑定。

## 审核门禁

`scripts/review_summary_queue.py` 增加两个可选入参：

- `--duplicate-map`：`set` 拒绝把影子副本写成证据，并直接给出应改用的 canonical
  ID；`show` 自动把影子提示归一化成 canonical 并折叠重复；`status` 输出
  `shadow_bound_rows`，非零时退出码为 1。
- `--evidence-windows`：`show` 优先使用因果窗口的候选，并打印窗口种类、区间、
  canonical 事件数与覆盖的会话数。未提供时明确提示当前显示的是可能包含未来
  事件的基线提示。

推荐的逐条审核命令：

```
python scripts/review_summary_queue.py show \
  --queue D:\silence\backups\stage-e-summary-review-working.jsonl \
  --raw-db D:\silence\backups\raw_events.sqlite \
  --duplicate-map D:\silence\backups\stage-e-raw-duplicate-map.json \
  --evidence-windows D:\silence\backups\stage-e-summary-evidence-windows.json \
  --next-pending
```

## 边界

- 归档、摘要 artifact 与基线队列 `stage-e-summary-review-queue.jsonl` 只读。
- 候选与窗口都只是定位辅助，不构成证据绑定；`keep` 与 `rewrite` 仍需人工读过
  原文后显式绑定至少一个 canonical raw event。
- 影子映射只服务于审核、导入与 embedding 阶段的去重判断，不用于删除归档数据。
- 在 842 条全部审阅且整体校验通过前，不实现也不执行 summary 导入。