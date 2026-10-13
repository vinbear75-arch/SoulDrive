# SoulDrive 记忆审核并发幂等优化

日期：2026-09-09

## 结论

本轮发现并修复了记忆审核的一个真实并发缺口：顺序重试已有 `review_id + decision_digest` 重放机制，但两个会话同时接受同一候选时，都可能先读到 `pending`，随后各自插入记忆；旧实现最终会出现一个成功、另一个撞上唯一约束，或者在不同存储条件下产生重复事实。

现在 `LearningStore.decide_review()` 在读取待审核状态、接受候选、创建记忆/技能和写入最终决策之间使用 SQLite `BEGIN IMMEDIATE`。第二个并发调用会在第一个提交后重新读取状态，并按相同决策摘要走既有 replay 路径；它不会再重复创建 memory。

本轮没有把“语义相近但内容不同”自动合并成同一事实。那仍需实体/事实槽位识别与冻结评测，继续保持人工审核边界。

## 外部实践对照

- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)将 episode 按 `group_id` 顺序处理，并保留 `valid_at/invalid_at` 与来源；可迁移的不变量是同一记忆范围的写入需要可排序、可回放，而不是无条件追加。
- [Graphiti edge operations](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py)把新事实解析为已有边的 duplicate/invalidation/new 三类结果；SoulDrive 当前只借鉴“写入前先确定状态”的原则，不在小项目中引入图数据库或自动事实合并。
- [Temporal fixed-count retries](https://github.com/temporalio/documentation/blob/main/docs/design-patterns/fixed-count-retries.mdx)说明外部调用在结果尚未记录时可能被再次执行，因此恢复/重试必须依赖稳定幂等键；这里的 `decision_digest` 继续承担同一审核决策的稳定身份。

## 代码改动

- `core/learning_store.py`
  - 在 `decide_review()` 内进入 `BEGIN IMMEDIATE` 后重新读取 `reflection_reviews`，避免事务外的旧 `pending` 快照决定写入。
  - 已完成审核且摘要、项目范围一致时直接复用 `_replay_review_decision()`；摘要或项目范围不同仍拒绝重试。
  - 记忆、技能、派生 `learning_chunks` 与审核状态仍在同一 SQLite 事务中提交。
- `tests/test_learning_store.py`
  - 新增双连接并发接受回归，要求结果恰好为一次首次提交、一次 replay，并且最终只有一条 memory。

## 失败优先与验证

- 旧实现回归结果：一个调用成功，另一个调用出现 `IntegrityError('UNIQUE constraint failed: memories.memory_id')`。
- 修正后定向双测试：`test_concurrent_review_decision_is_serialized_and_replayed` 与既有顺序幂等测试均通过。
- 记忆与学习全链路：`tests/test_learning_store.py tests/test_memory_gate.py tests/test_learning_validator.py tests/test_mcp_server_learning.py`，`88 passed, 1 warning`。
- 警告仍是环境中的 Starlette/httpx 弃用提示，不是本轮失败。

## 未完成边界

- SQLite 写事务只解决同一工作区的本机并发，不提供跨主机分布式锁。
- 相同候选被不同 `review_id` 或不同内容表达时，仍依赖现有 duplicate/conflict 检测与人工审核；本轮没有未经评测地加入 LLM 自动合并。
- 文件归档、PostgreSQL 和 Redis 向量同步仍是审核事务之后的派生同步；同步失败会返回 degraded，重试依靠 memory/skill ID 的 upsert 语义，不宣称跨存储 exactly-once。
