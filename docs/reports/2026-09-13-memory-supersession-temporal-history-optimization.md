# 记忆 supersede 的时间线与历史可查询性优化

日期：2026-09-13

## 发现的问题

审计记忆更新路径时发现，普通 `update_memory()` 和 `archive_memory()` 会写入 `memory_versions`，但审核确认的新事实 supersede 旧事实时只把旧父记录、搜索 chunk 和历史版本标记为 archived。结果是：当前查询能看到新事实，但按旧时间点查询无法再看到被替代前的事实；同时新事实没有显式 `valid_from` 时会被当作从时间线起点就成立。

这会破坏“事实当前状态”和“事实在过去何时成立”的区分，也让项目比较、历史实验解释和记忆审计无法复现当时状态。

## 外部实践对照

- [Graphiti memory MCP implementation](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 明确记录 episode 的写入时间与 `reference_time`，事实使用 `valid_at/invalid_at` 保留时间范围；本地实现沿用这个最小原则，不引入图数据库。
- [Mem0 memory concepts](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx) 将 SQL 事实层作为 source of truth，并把 update/delete 作为显式操作；本轮继续保留加密 SQLite 为权威层，向量/关键词同步仍是派生投影。

## 实现

- `core/learning_store.py` 新增受事务保护的 `_preserve_memory_version_locked()`，在 supersede 归档旧父记录前，把旧事实当前版本写入 `memory_versions`，区间为原 `valid_from`（没有则用创建时间）到 supersede 边界。
- 新事实如果确实 supersede 了旧事实且没有显式 `valid_from`，从同一个 supersede 边界生效，避免历史查询同时返回新旧两个互相冲突的事实。
- 旧父记录和当前搜索 chunk 仍然归档，当前上下文不会重新召回旧事实；旧 `memory_versions` 保持 active 仅用于其有效时间区间内的 `as_of` 查询，不恢复为当前记忆。
- 新增失败优先测试 `tests/test_learning_store.py::test_conflict_supersession_preserves_point_in_time_history`，同时覆盖旧时点只返回旧事实、当前时点只返回新事实。

## 结果

- 记忆、学习存储、会话持久化和时间范围回归：`151 passed`。
- 记忆质量评测：`6/6` 场景、`7/7` 查询通过，`memory_recall_at_k=1.0`，过期/越权泄漏率 `0.0`。
- 代码变更后的完整 `scripts/check.ps1`：后端 `1352 passed, 1 warning`，前端构建及 8 组 UI 测试 `61 passed`，`compileall` 与 `cargo check --locked` 通过。

## 边界

- 该修复证明本地加密记忆层的编辑、撤销和 supersede 都能保留可查询的时间区间，不证明自动记忆抽取的事实判断、冲突分类召回质量或长期多模态记忆质量。
- `memory_versions` 是历史查询投影，不能替代审计事件、用户确认或实验账本；事实因果和业务正确性仍需来源证据与人工审核。
- 未引入 Graphiti/Mem0 等外部服务，也未声称跨进程事务、外部 exactly-once、向量同步崩溃恢复或云端记忆已完成。
