# 生产替换前评估报告

更新时间：2026-08-30
评估对象：Zeabur `ombre-staging`（Haven + Gateway + Xinchao sidecar）
Staging 地址：`ombre-staging-6087df.zeabur.app`

## 结论

**不建议现在用 staging 替换生产 `ombre-brain` 或 `xinchao-nian-caric`。**

本轮已证明 staging 的隔离拓扑、进程端口、密钥持久化、Haven health、Gateway 的 OpenAI/Anthropic 双协议（流式/非流式）、Claude Code 工具调用续轮、Xinchao context/continuity/event 与 MCP initialize 均能工作；但它仍是“运行链路验收通过、长期记忆尚未就绪”的空库 staging，不具备生产替换所需的数据完整性和功能等价性。

建议保留当前 staging 作为继续迁移和回归环境，完成摘要迁移、向量重建、真实语料召回、卷恢复演练和生产等价配置复核后，再重新做一次 go/no-go 评估。本报告不执行任何生产切换、重启、Redeploy、迁移或生产卷操作。

## 1. 已确认通过的事项

| 范围 | 证据 | 结论 |
|---|---|---|
| 仓库隔离 | Haven `codex/miss-compat`、Xinchao `codex/vnext-experiment`、Gateway `codex/quiet-memory-gate` 工作树均 clean；生产服务和生产卷未触碰 | 通过 |
| staging 拓扑 | 服务 `ombre-staging`，Service ID `service-6a918709b7ff62ee8d7ff00d`；独立 `/data`、`/state` 卷；入口代理分流 Haven/Gateway/Xinchao | 通过 |
| Haven 公网 health | `GET /health` 返回 200、`status=ok`；reflection、portrait、persona 与 domain sentinel 的 `api_ready=true` | 通过 |
| Gateway 公网 health | `GET /v1/health` 返回 200；Gateway token 已配置，8 个 upstream 均 `ready=true`，adapter `reachable=true` | 通过 |
| Xinchao 公网 health | `GET /xinchao/health` 返回 200，`ok=true`，版本 `2.5.12` | 通过 |
| OpenAI 协议 | 非流式 200、`finish_reason=stop`；流式为 `text/event-stream`，以 `[DONE]` 收尾 | 通过 |
| Anthropic 协议 | `/v1/messages` 非流式 200；流式事件序列完整；顶层 `system` 约束生效 | 通过 |
| 真实 Claude Code | Claude Code 2.1.251 实际请求 staging；单轮与工具调用续轮均成功，`permission_denials=0` | 通过 |
| Xinchao 联动 | `/v1/context?mode=turn`、`/v1/continuity/sync`、`/v1/conversation-event` 均返回 200；outbox delivered，failed=0 | 通过 |
| 密钥持久化 | 面板密钥落在 `/state/.env`，权限 `0600`；三个 sidecar 进程均能读取；Gateway upstream hot update 已验证 | 通过 |
| 原始对话归档 | `raw_events.sqlite` 完整性检查为 `ok`；有效原文 11,884 条，FTS 同步 11,884 条，0 duplicate | 通过 |

上述运行时证据和测试记录详见 `D:\silence\.codex\STATE.md`、`D:\silence\.codex\WORKLOG.md` 以及 `D:\silence\backups\stage-e-apply-report.json`、`D:\silence\backups\stage-e-verify-report.json`。

## 2. 阻塞替换的事项

### 2.1 长期记忆为空，尚未具备真实召回验收条件（P0）

当前 staging `/health` 和 `/v1/health` 的 bucket 计数均为 0：

- `permanent_count=0`
- `dynamic_count=0`
- `feel_count=0`
- `archive_count=0`
- `total_size_kb=0`

因此本轮虽然证明了“请求可以经过记忆链路”，但没有证明真实长期记忆能够正确写入、embedding、召回、去重、按权限过滤或与 raw evidence 融合。不能把协议层通过误认为记忆库迁移完成。

### 2.2 842 条 legacy summary 尚未完成 keep/rewrite/merge/reject（P0）

`D:\silence\backups\stage-e-summary-artifact.json` 已生成 842 条摘要的不可变中间产物，但它不是可直接导入生产的最终记忆：

- 842 条的 `decision` 仍全部为空；
- `legacy_review_status` 为 `backlog=739`、`candidate=103`；
- 842 条的 `evidence_confidence` 仍为 `low`；
- 所有条目的 `source_event_ids` 均为空；
- 尚未完成证据归组、最终正文/元数据审核、bucket provenance 和当前 embedding outbox 重建。

在这些步骤完成前，不应把摘要中间产物写入 staging 长期 bucket，更不能写入生产。

### 2.3 staging 配置并非生产等价配置（P1）

为保证隔离和启动稳定，staging 明确关闭了 `OAUTH_ENABLED`、`DASHBOARD_ENABLED`、`BRIDGE_ENABLED`、`MODEL_ENABLED` 等可选入口。因而 staging 的协议和核心 Gateway 路径已验证，但不能据此宣称生产侧 OAuth、Dashboard、Bridge、模型入口及其权限边界也已完成替换验收。

### 2.4 持久卷清理与恢复闭环尚未完成（P1）

已知两个需要人工决策/后续处置的残留：

1. `/data/.raw_events.sqlite.upload` 为 0 字节历史占位文件，可在确认无并发上传后删除；
2. `/state/.env.bak.20260830` 为 240B 的旧备份，含 4 个幸存密钥，是否保留需由严槿决定。若不保留，应在确认 `/state/.env` 可用且已有安全备份后清理，并再次核验没有敏感残留进入日志、Git 或报告。

此外，当前证据证明了文件 hash 和 SQLite 完整性，但尚未证明从 Zeabur 持久卷快照执行过一次可复核的恢复演练；这仍是生产门禁风险。

## 3. 数据迁移边界与解释

### 3.1 Raw archive 已完成，但不是长期记忆

原始 CSV 的合同基线为 11,919 行；按 `load_chat_manifest()` 的角色和正文校验，11,884 行是有效 source rows，35 行被判为无效（`invalid_role=14`、`empty_text=21`）。正式 apply 的数据库账目为：

- `input_rows=11919`
- `valid_source_rows=11884`
- `inserted_rows=11884`
- `duplicate_rows=0`
- `rejected_rows=0`（这是 DB 插入层计数，不覆盖 manifest 校验层）
- 12 批全部 finished

这解释了 11,919 与 11,884 的差额：不是传输丢失，而是源侧校验过滤。Raw archive 继续保持无 embedding、无 bucket、默认不进入普通 breath 的隔离边界。

### 3.2 Summary migration 仍是未完成的 E2

842 条摘要目前只是审计和审核输入。下一阶段必须先完成逐条处置和证据绑定，再通过 Haven 的正常 bucket 写入与 embedding outbox，最后以真实语料执行召回噪声、日期/原句证据、权限和删除恢复回归。

## 4. 风险清单

| 优先级 | 风险 | 影响 | 关闭条件 |
|---|---|---|---|
| P0 | 真实 bucket 为空 | 替换后用户会看不到已审核的长期记忆 | 摘要完成处置并导入；bucket/embedding/召回计数与抽样证据齐全 |
| P0 | 摘要没有证据绑定和最终决策 | 可能把低置信或重复摘要错误固化 | 842 条均有 keep/rewrite/merge/reject 决定；rewrite 仅引用允许的 legacy/raw 证据 |
| P0 | 尚未完成最终 embedding | 语义召回不可证明，模型混用风险未关闭 | 当前模型 outbox 全部完成或明确可接受的降级账目；重启后可恢复 pending |
| P1 | staging 功能开关少于生产 | 不能证明生产入口和权限等价 | 按生产配置逐项做隔离副本验收，不复用生产 token/卷 |
| P1 | 卷恢复未演练 | 故障时可能无法恢复 raw、bucket、配置和 outbox | 新快照、hash、恢复步骤和一次无损恢复演练均有记录 |
| P1 | 临时文件/旧密钥备份残留 | 增加泄露面和运维误用风险 | 完成人工决策、清理、权限复核和敏感扫描 |
| P2 | staging 当前持续承载实验写入 | 后续审核可能混入测试数据 | 使用 `test_data` 或独立实验目录，迁移前固定快照并记录基线 |

## 5. 替换前必须完成的验收门槛

1. 完成 842 条摘要的逐条审核和处置，生成不含私密正文的统计报告；
2. 将保留/重写后的摘要通过 Haven 正常写入路径导入 staging，确认 provenance、source_ranges、merge/keep-both、test_data 边界正确；
3. 完成 embedding outbox 重建并验证重启恢复、重复任务、失败重试和最终计数；
4. 在真实导入语料上完成普通事实/偏好/项目召回、日期/原句 raw archive 专用召回、锁信/Source/Relation 过滤和删除恢复抽样；
5. 按生产等价开关建立隔离配置副本，完成 OAuth、Dashboard、Bridge 以及 MCP 权限的专项验收；
6. 生成 staging 数据卷快照，做一次恢复演练，并确认 `/state/.env`、runtime config、raw archive、bucket、embedding outbox 的闭环恢复；
7. 清理或明确保留两个已知残留文件，完成敏感信息扫描；
8. 重新执行一次最终全量验证并产出 `acceptance-report.md`、`difference-report.md`、`migration-report.json`、rollback 清单和本报告更新版；
9. 由严槿明确批准后，另行制定生产切换和回滚窗口。本任务不自动执行切换。

## 6. 建议

- **现在：NO-GO。** 不替换生产，不重启生产，不迁移生产卷。
- **保留 staging：GO。** 当前 staging 适合作为隔离的协议、Gateway、Xinchao 和迁移开发验证环境。
- **下一工作动作：** 只在 staging 上完成 E2 摘要审核/证据绑定和 embedding 重建；完成后更新本报告，不重复已经通过的 F 阶段协议验收。

## 附录：本报告未宣称的事项

本报告不宣称以下事项已经完成：

- 842 条摘要已经成为可召回的长期记忆；
- staging 与生产的所有可选入口和权限配置完全等价；
- 已经执行生产切换、生产迁移或生产卷恢复；
- 已经清理上述两个残留文件；
- 已经完成一次可证明的持久卷恢复演练。
