# 记忆来源工作流 provenance 优化

日期：2026-09-14

## 结论

本轮修复了记忆来源链路的一个可审计性缺口：反思草稿从绑定研究会话生成普通 `project_fact` 或 `user_preference` 时，原实现只保存 `source_session_id`，不会保存该会话所属的 `workflow_id`。记忆落库后仍能知道“来自哪个会话”，但无法知道“来自哪个研究窗口”，不同会话窗口的来源角色因此不完整。

现在，绑定会话的候选会保存独立的 `source_workflow_id`。该字段沿候选加密载荷、当前记忆、历史版本和检索投影传递，并在 `source_trace.workflow_id` 中重复提供可读来源。它只描述来源，不占用 `workflow_fact` 的工作流命名空间；普通项目事实不会因此被错误限制到某个窗口。

## 外部实践对齐

- [Mem0 memory operations](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/add.mdx) 将 `user_id`、`agent_id`、`run_id` 和可过滤 metadata 作为记忆作用域/来源的一部分；本项目采用同一原则，但使用本机已有的 `project_id`、`source_session_id` 和新增 `source_workflow_id`，不引入 Mem0 运行时。
- [Mem0 memory implementation](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) 在写入和查询时同时构造作用域 metadata 与 query filters；本项目保留“来源字段”和“检索命名空间”分离，避免把普通事实误当成 workflow-scoped memory。
- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 用 `group_id`、episode UUID 和 reference time 组织可追溯记忆，并支持回指来源 episode；本项目本轮只补齐来源工作流标识，时间有效区间与 message ID/sequence 仍沿用既有字段。

## 实现边界

1. 只补充反思候选到记忆投影的 provenance，不改变 `workflow_fact` 的过滤规则、不扩大项目可见范围，也不把来源工作流当作记忆内容。
2. 旧版加密载荷没有 `source_workflow_id` 时仍可正常读取；只有新候选或后续编辑会携带该字段。
3. 本轮不宣称自动记忆抽取质量提升、跨模型泛化、真实 Redis/PostgreSQL schema 迁移、跨存储 exactly-once 或真实 Tauri/WPS UI/E2E 已完成。

## 回归证据

- 失败优先：新增普通 `project_fact` 的绑定研究窗口用例，修复前候选响应缺少 `source_workflow_id`；修复后该用例通过，并验证接受后的 `source_trace.workflow_id`。
- 记忆/学习/会话组合回归：`226 passed, 1 warning`。
- 完整工程门禁：后端 `1518 passed, 1 warning`；前端生产构建成功；8 组 UI 脚本合计 `62 passed`；Python `compileall` 与锁定 Cargo 检查通过。前端仍有既有的单 chunk 大小警告。
