# 历史摘要导入与未来原文策略

更新时间：2026-08-31

## 当前决策（以此为准）

用户已明确取消历史原文迁移。后续不再执行 Supabase 原文导出、补齐、raw archive 迁移、摘要增量 apply 或人工审核流程。

当前只保留一个运行时目标：**从现在开始保存新的 user/assistant 原文，并在需要时通过专用 raw 搜索召回；原文逐字保留，但不进入普通长期记忆 bucket、embedding 或普通 `breath`。**

## 未来原文链路

### 保存

- `Ombre-Brain/raw_events.py` 的 `RawEventStore` 使用 SQLite `raw_events` 表保存原文。
- 只接受 `user` 与 `assistant` 角色；system、developer、tool、tool result 和已识别的 memory/client injection 会拒绝。
- 原文正文按输入保存，不做摘要或改写；客户端附件和工作区上下文不会混入存档正文。
- `(source, source_event_id)` 与内容哈希提供幂等去重，重试不会制造重复事件。
- `gateway.py` 在完整可见对话轮次结束后镜像 user/assistant 原文，不保存半截 assistant。

### 专用召回

受保护接口位于 `server.py:11530-11596`：

- `POST /api/ingest-raw`：批量写入新的 user/assistant 原文。
- `GET/POST /api/search-raw`：按关键词或空查询召回，并支持 `source`、`role`、`conversation`、`session`、`since`、`until`、`limit` 筛选。

raw event 是独立的原文保险箱，不应被当作普通长期记忆使用。普通 `breath` 不主动返回 raw event；需要原文时走专用 raw 搜索或日期/原句专用路径。

## 本地验收证据

`D:\silence\Ombre-Brain\tests\test_raw_events.py` 覆盖：

1. 新原文逐字保存并可按关键词专用召回；
2. 相同事件重复提交只保存一次；
3. system 与注入上下文被拒绝。

已执行联合回归：

```text
72 passed in 4.66s
```

## 历史迁移结果（仅作封存证据，不是当前待办）

此前阶段 E/F 曾在 staging 完成历史数据处理：约 11,884 条有效历史 raw archive、990 条摘要导入（985 created、5 updated，后续续跑清除传输失败），并完成 Gateway/Xinchao/staging 主链路验收。相关脚本、导出物、state 和报告保留用于审计，但不再继续运行。

历史摘要导入原计划的 842 条人工审核、raw 证据绑定和后续增量 apply 均已停用。保留这些文件不代表仍要求执行迁移。

## 禁止操作

- 不要重新运行 `migrate_supabase_archive.py` 的历史 raw apply。
- 不要重新导出或补齐 Supabase 历史原文/摘要。
- 不要对旧 state 文件做摘要增量 apply。
- 不要重启、Redeploy 或修改生产服务及生产卷。
- 不要把 token、密码、完整聊天或敏感正文写入 Git、STATE、WORKLOG 或普通诊断。

## 后续验收（可选）

如需继续验收，只做一条新的 staging user/assistant 原文保存→`/api/search-raw` 专用召回，并检查普通 `breath` 不返回该 raw event；不以此恢复历史迁移计划。
