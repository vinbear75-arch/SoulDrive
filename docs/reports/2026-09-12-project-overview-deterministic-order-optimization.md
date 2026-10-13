# 项目总览列表确定性排序优化

日期：2026-09-12

## 结论

项目总览的三组列表现在都保留稳定业务身份作为末级排序键：实验运行按 `performed_at DESC, run_id ASC`，项目任务按到期时间、标题和 `task_id`，决策按 `decided_at DESC, decision_id ASC`。并列时间/标题时，项目总览不再依赖 SQLite 插入顺序。

## 外部实践对照

- [Temporal Visibility schema](https://github.com/temporalio/temporal/blob/main/schema/sqlite/v3/visibility/schema.sql) 在执行时间排序后保留 `run_id`，避免同时间工作流的可见性顺序不稳定。
- [LangGraph checkpointer 文档](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx) 将 checkpoint ID 作为可排序的持久身份，并把历史列表的顺序作为可观察契约。
- [LangGraph checkpoint conformance test](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint-conformance/langgraph/checkpoint/conformance/spec/test_copy_thread.py) 直接验证复制后的 checkpoint 顺序保持一致，说明恢复/调试列表不能把数据库返回顺序当作业务语义。

这些实现只提供“时间/业务字段之后保留稳定身份”的设计参考；SoulDrive 没有因此引入 checkpoint 框架或改变项目数据模型。

## 失败优先验证

新增 `tests/test_mcp_server_projects.py::test_project_overview_uses_stable_identity_tiebreakers`：逆序写入同时间的 `run-b/run-a`、同到期时间同标题的 `task-b/task-a` 和同决策时间的 `decision-b/decision-a`。

- 修复前最近实验返回 `run-b, run-a`，证明排序依赖插入顺序。
- 修复后分别按 `run_id`、`task_id`、`decision_id` 稳定返回；项目范围、数量限制、展示字段和健康计数没有改变。

## 回归证据

- 项目 API 与项目目录定向回归：`39 passed, 1 warning`。
- 全量 `scripts/check.ps1`：后端 `1161 passed, 1 warning`；前端生产构建与 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮只证明项目总览列表在并列排序字段下可重复，不证明实验选择语义、指标准确率、任务调度、跨存储 exactly-once、分页游标或真实 UI/E2E。
