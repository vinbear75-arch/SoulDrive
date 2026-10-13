# 聊天与运行时记忆回执完成栅栏优化

日期：2026-09-15  
范围：`/chat` 请求回执、`/learning/memories/runtime` 幂等回执及租约丢失后的可恢复状态。

## 外部实践与当前缺口

本轮对照 [DBOS workflow recovery](https://docs.dbos.dev/production/workflow-recovery)、[Restate HTTP invocation idempotency](https://docs.restate.dev/services/invocation/http) 与 [Temporal Workflow ID/Run ID](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-execution/workflowid-runid.mdx)：持久运行记录需要区分业务结果、当前执行租约和可重放回执；租约丢失后，迟到 worker 不能把未确认提交伪报成完成，已持久化的业务结果则应通过稳定身份恢复，而不是重复生成或静默丢失。

SoulDrive 已经为聊天和运行时记忆建立了 `request_id`、输入摘要、租约和完成回执，但两个完成闭包都忽略了 `complete_request()` 的布尔结果。于是可以复现：回答/记忆已经写入事实源，回执仍是 `in_flight`，而审计或接口仍把本次请求当作已完成。

## 失败优先与最小改动

- `/chat` 完成回执现在读取并保留条件更新结果；租约令牌失效、回执缺失或完成处理器异常时，写入 `chat.request_receipt_completion_lost`，`chat.completed` 明确带 `request_receipt_completed=false`。
- 聊天回答已经安全落盘时仍允许返回该回答；这不是 exactly-once，而是“业务结果可恢复、回执未确认”的明确状态。相同请求后续可通过助手消息的完整输入摘要重放并重新完成回执。
- 聊天旧回答重放不再审计为无条件 `completed`，而是使用 `response_replayed_receipt_pending` 标记回执仍待确认。
- 运行时记忆保存同样读取完成结果；业务资产已落盘但回执未确认时返回明确 warning，并写入 `learning.runtime_memory_receipt_completion_lost`，不把已保存结果回滚或伪造为未保存。

## 回归证据

失败优先测试覆盖：

- 正常聊天回答完成后租约丢失；
- 带旧助手回答的幂等重放在回执 claim 后丢失租约；
- 运行时记忆资产保存后回执完成被栅栏拒绝；
- 原有已完成聊天回放路径保持不加载 RAG。

结果：

- 新增/相关回归：`4 passed, 1 warning`；
- `tests/test_mcp_server_learning.py tests/test_conversation_store.py`：`219 passed, 1 warning`，约 230 秒；
- 完整工程门禁：后端 `1580 passed, 1 warning`（约 365 秒），前端生产构建成功、UI 脚本 `62 passed`，Python `compileall` 与锁定 Cargo 通过。

## 边界

- 这保证的是本机事实源、回执租约和可重放身份之间的状态诚实，不是跨主机 exactly-once。
- 外部副作用或模型生成在事实源提交前后崩溃时仍可能需要 at-least-once 恢复；必须继续依赖事件/请求身份幂等。
- 当前回归使用本机 SQLite/加密事实源和可控故障注入，未宣称真实 Redis/PostgreSQL、长时间进程崩溃、打包 Tauri 或真实 UI/E2E 已验收。
