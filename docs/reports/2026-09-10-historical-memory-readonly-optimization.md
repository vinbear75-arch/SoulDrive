# 历史记忆读取只读化优化记录

日期：2026-09-10

## 结论

本轮修复了记忆时间轴的一处隐性副作用：带 `as_of` 的历史检索虽然读取的是过去时点的有效事实，却仍会把命中的记忆写入当前 `last_used_at`。这会让“回顾上个月”改变今天的记忆新鲜度排序，也会让历史读取反过来影响当前上下文。现在历史记忆上下文保持只读；只有实时上下文才记录当前使用时间。

## 外部实践对照

- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)把事实的 `valid_at`/`invalid_at` 与 episode 的摄取时间分开，并支持按时间范围检索；历史读取不应把摄取/使用时间混写成事实发生时间。
- [Graphiti core](https://github.com/getzep/graphiti/blob/main/graphiti_core/graphiti.py)将 episode 的事件时间作为 temporal watermark，并强调时间水印不能回退；SoulDrive 保留现有 `valid_from`/`invalid_at`，本轮进一步阻断历史查询对当前使用投影的回写。
- [Mem0 advanced memory operations](https://github.com/mem0ai/mem0/blob/main/docs/platform/advanced-memory-operations.mdx)把搜索过滤和显式 `update` 分开；检索不应隐式变更事实内容。SoulDrive 同样不让历史检索隐式变更当前记忆生命周期或排序状态。

## 失败优先验证

- 新增 `test_historical_memory_context_does_not_touch_current_usage_timestamp`：构造历史时间查询和命中的记忆，修复前 `touch_memories()` 会收到该记忆 ID，回归失败；修复后不再触碰。
- 保留并复跑 `test_learning_context_routes_previous_month_to_memory_as_of`，确认历史范围仍然传给列表、关键词检索和语义检索。
- `tests/test_mcp_server_learning.py`：`66 passed, 1 warning`。
- 会话/学习/MCP/TaskJournal 组合：`174 passed, 1 warning`。
- 当前最新 `scripts/check.ps1`：后端 `1123 passed, 1 warning`；前端生产构建成功，7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 实现

- `core/mcp_server.py::_learning_context_for_query` 在 `memory_as_of is None` 时才调用 `touch_memories`。
- 历史读取仍保留 `memory_time_scope`、`as_of`、`valid_from`/`invalid_at` 和来源追踪；变化只针对使用计时副作用，不扩大或缩小历史召回范围。
- 实时查询的使用计数与当前行为保持不变。

## 边界

- 本轮不新增 bi-temporal 数据库字段；现有 `valid_from`/`invalid_at` 承担事实有效区间，`created_at`/`updated_at` 仍承担本地记录时间。
- 这只阻断历史问答链路的 `last_used_at` 回写，不改变用户明确编辑、审核接受、撤销或删除记忆的写入行为。
- 语义记忆排序和答案质量仍需在冻结业务集上评测；本轮证明的是时间语义和副作用边界，不是准确率提升。
