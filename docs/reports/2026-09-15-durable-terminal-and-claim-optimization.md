# Durable execution 终态投影与并发 claim 优化

日期：2026-09-15  
范围：TaskJournal 工作流恢复、索引运行 receipt、项目/文档删除 receipt

## 外部实践与真实缺口

本轮对照了三个成熟实现的共同约束：

- [DBOS 工作流升级](https://docs.dbos.dev/python/tutorials/upgrading-workflows) 要求恢复时使用兼容的工作流定义；定义不兼容时不能把恢复失败伪装成普通业务失败。
- [DBOS 工作流恢复](https://docs.dbos.dev/architecture) 将工作流输入、步骤结果和终态作为持久 checkpoint，并在恢复时从最后一个完成步骤继续。
- [Restate HTTP invocation](https://docs.restate.dev/services/invocation/http) 用持久化幂等键回放同一调用结果；[Temporal Workflow ID/Run ID](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-execution/workflowid-runid.mdx) 将业务身份与单次运行身份分开。

当前代码审计发现四处可复现缺口：

1. TaskJournal 在缺少旧步骤处理器时先 claim 任务，再把“当前代码定义不兼容”写成 `failed`，会破坏可恢复性。
2. 索引 worker 的成功 no-op 状态是 `idle`，磁盘不足状态是 `blocked`，但 IndexRunJournal 只接受 `completed/failed` 终态；两者都会使幂等 receipt 停在 `queued`。
3. 项目删除 receipt 的首次创建是先查后插；两个进程同时 claim 同一项目时，失败者会遇到 SQLite 主键冲突，而不是复用同一 receipt。
4. 文档删除 receipt 也采用先查后插；两个进程同时 claim 同一文档版本时，失败者会遇到 SQLite 主键冲突，而不是进入既有租约/回放分支。

## 失败优先与最小实现

- `TaskJournal.run_steps()` 在 claim 前检查所有未完成步骤是否都有可调用处理器；缺失时抛出 `TaskDefinitionMismatchError`，不修改任务和步骤状态。已完成步骤不要求旧处理器存在，保证从 checkpoint 继续。
- `_sync_index_run_receipt()` 将索引 `idle` 映射为成功 `completed`，将 `blocked` 映射为带安全错误观察的 `failed`；运行时 UI 仍保留原始 `idle/blocked` 语义。
- `ProjectCatalog.begin_project_deletion()` 使用 `ON CONFLICT(project_id) DO NOTHING` 后回读唯一 receipt；并发请求共享同一 snapshot，已完成 receipt 不回退，失败 receipt 仍可进入下一次恢复。
- `ProjectCatalog.claim_document_deletion()` 使用 `ON CONFLICT(idempotency_key) DO NOTHING` 原子占位；冲突请求回读既有 receipt，继续执行输入 digest、完成态和租约判断，不把并发重试暴露为唯一键异常。

## 回归与工程门禁

- 先失败后通过：缺少处理器、已完成步骤兼容恢复、索引 `idle`/`blocked` 终态投影、并发项目删除 claim、并发文档删除 claim。
- durable 组合：TaskJournal `29 passed`；索引幂等 `14 passed`；项目目录与项目 API `65 passed`。
- 本轮完整工程门禁：后端 `1573 passed, 1 warning`；前端生产构建成功、UI 脚本 `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

本轮只证明本机 SQLite receipt、任务恢复前置检查和运行状态投影的确定性；不宣称跨主机 exactly-once、真实 Redis/PostgreSQL 事务、外部 SMTP/日历提供商的 exactly-once、真实 Tauri/WPS UI/E2E 或长历史性能已完成验收。TaskJournal 的处理器版本仍是显式兼容检查，不等同于完整 Temporal/DBOS 工作流版本编排。
