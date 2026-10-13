# SoulDrive 聊天完成态与耐久执行边界优化

## 结论

本轮没有引入 Temporal、Restate 或新的任务框架，而是把当前本机单用户链路中已经存在的“回答生成”和“耐久完成”拆成两个阶段：只有助手消息归档以及会话摘要压缩均成功后，聊天才会被记为 `chat.completed`。回答生成后若归档失败，接口仍返回已有的可恢复错误提示，并只记录 `chat.failed`。

## 外部实践对齐

- Temporal 的工作流通过事件历史恢复，Workflow 只有在持久步骤完成后才能形成可继续的状态；Activity 需要按重试设计为幂等或明确不可重试：[Temporal architecture](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md)、[Retry policies](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx)。
- Restate 将可靠执行拆为可恢复步骤，并要求非幂等入口使用幂等键，避免重试产生重复副作用：[Restate](https://github.com/restatedev/restate)、[Safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.4.md)。

SoulDrive 当前只需要修正完成判定的边界，不需要把一个本机同步 SSE 流程改造成重量级分布式工作流。

## 发现的问题

原先 `core/mcp_server.py` 在生成回答后立即设置 `success = True`，随后才执行：

1. 助手消息写入加密会话归档；
2. 幂等请求回执完成；
3. 会话摘要压缩与归档。

第 1 或第 3 步失败时，异常处理会记录 `chat.failed`，但 `finally` 仍可能依据旧的 `success=True` 再记录 `chat.completed`。这会使运行指标、审计事件和可恢复状态不一致。

## 最小修正

- 将 `success = True` 移到助手消息归档、请求回执完成和摘要压缩之后。
- 保留现有错误处理、失败回执释放和错误提示，不改变无会话聊天的响应契约。
- 新增回归：模拟助手归档失败，确认只出现失败事件、延迟指标为失败，不能出现完成事件。

## 验收证据

- 定向回归：`tests/test_mcp_server_learning.py -k "assistant_archive_fails or chat_learning_context or chat_replays_completed_request or chat_rejects_duplicate"`，`3 passed, 1 warning`。
- 本轮之后的全量 `scripts/check.ps1`：当时后端 `1001 passed, 1 warning`；后续答案来源可追溯性、TaskJournal fencing、聊天历史时点路由、记忆版本历史、工作流文档范围、中断 fencing、邮件预留竞态、长步骤恢复保护、记忆审核并发和自动记忆写入边界循环的最新结果为后端 `1019 passed, 1 warning`；前端生产构建通过、7 组测试共 `44 passed`；Tauri `cargo check --locked` 通过。唯一已知警告是 Starlette/httpx 的弃用提示，不是产品测试失败。

## 明确边界

- 这保证的是本机聊天链路的完成态与归档状态一致，不宣称跨进程、跨主机的 exactly-once。
- 外部 Redis/PostgreSQL 写入仍依赖现有 outbox 和各自的幂等 upsert/消息 ID；真实服务故障恢复、打包启动和真实模型质量仍需独立环境验证。
- “Agentic RAG”仍是有边界的检索路由与一次纠偏重试，不是无上限的自主循环。
