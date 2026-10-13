# 历史记忆 freshness 时点一致性优化

## 结论

本轮修复了记忆时间路由与上下文门之间的一个真实不一致：学习存储已经按历史 `as_of` 过滤 `valid_from/invalid_at`，但 `gate_memory_context` 仍默认使用当前时间计算 freshness。这样“回顾上个月”会把当前时点的访问时间带入历史判断；历史记忆后来被重新使用时，可能被错误地当作历史时点的新鲜记忆。

现在历史查询把解析出的 `memory_as_of` 传给记忆门；freshness 只使用不晚于该参考时点的 `last_used_at`、`confirmed_at` 或 `created_at`，参考时点之后的访问时间被忽略。普通查询仍使用当前时间，不改变现有衰减规则。

## 外部实践对照

- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)：事实同时保留 `valid_at/invalid_at`，搜索接口支持按事实时间范围过滤；检索时点不能只看当前数据库状态。
- [Graphiti edge maintenance](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py)：新旧事实的有效期关系需要显式处理，失效时间不能由当前访问时间隐式覆盖。
- [Mem0 memory retrieval](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx)：将 semantic、keyword、entity 与 temporal 信号分开；时间问题要使用时间意图和时间元数据，而不是只按当前相关性排序。
- [Letta archival memory search](https://github.com/letta-ai/letta/blob/main/letta/functions/function_sets/base.py)：归档检索提供显式起止时间过滤，并在结果中保留时间与 ID 元数据。

本项目不引入外部记忆服务，只让现有本地 SQLite 的时态过滤与本地 freshness gate 使用同一个参考时点。

## 实施

- `core/memory_runtime.py`
  - `gate_memory_context` 新增可选 `reference_timestamp`。
  - `_memory_freshness_weight` 在历史参考时点下忽略未来的 `last_used_at/confirmed_at/created_at`，避免未来访问改变过去的记忆状态。
- `core/mcp_server.py`
  - `_learning_context_for_query` 将已解析的 `memory_as_of` 传入记忆门。
  - 保持项目/全局范围、有效期过滤、冲突裁决、source trace 和现有衰减区间不变。
- `tests/test_memory_runtime.py`
  - 新增历史参考时点 freshness 回归。
  - 新增“未来访问时间不能污染历史判断”回归。

## 验证

- 新回归先在旧实现上失败：`gate_memory_context` 不接受历史参考时点，且未来 `last_used_at` 会使历史 freshness 保持 `1.0`。
- 修复后记忆、时间解析、学习存储和学习 API 回归：`114 passed, 1 warning`。
- 全量 `scripts/check.ps1`：后端 `1071 passed, 1 warning`；Python compileall、前端生产构建、7 组 UI 测试共 `48 passed`，Tauri `cargo check --locked` 均通过。

## 边界

- 这是时点一致性修复，不是新的记忆排序模型；衰减区间仍为现有本地启发式，不宣称长期记忆事实正确。
- `valid_from/invalid_at` 仍是事实有效性门；freshness 只影响通过时态过滤后的候选排序/准入，不替代事实失效判断。
- 不建设自动删除或自动改写记忆；明确冲突仍走已有人工审核、版本历史和受限 supersession 流程。
