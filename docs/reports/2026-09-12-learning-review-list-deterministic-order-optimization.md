# 待审核记忆列表确定性排序优化

日期：2026-09-12

## 结论

`LearningStore.list_pending_reviews()` 的全局和项目两条查询分支现在都使用 `created_at DESC, review_id ASC`。同一创建时间的待审核项不再依赖 SQLite 插入顺序，人工审核列表和重复打开后的顺序可复现；项目范围过滤、`pending` 状态过滤和候选计数没有改变。

## 外部实践对照

- [Temporal Workflow ID/Run ID](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-execution/workflowid-runid.mdx)：用稳定的业务级 ID 识别逻辑执行，把可变化的运行 ID 与业务身份分开，并用唯一性约束避免重复执行。
- [LangGraph Human-in-the-loop](https://github.com/langchain-ai/docs/blob/main/src/oss/langchain/human-in-the-loop.mdx)：人工中断需要持久化状态和稳定 `thread_id`，恢复不能依赖一次进程内的列表位置。
- [Letta memory schema](https://github.com/letta-ai/letta/blob/main/letta/schemas/memory.py)：记忆搜索结果显式返回唯一 ID 和时间字段，调用方可以基于结构化身份处理结果，而不是把展示顺序当作身份。

这些项目没有直接要求 SoulDrive 引入对应框架；本轮只迁移“稳定身份 + 明确排序键 + 可恢复人工操作”的最小机制。

## 失败优先验证

新增 `tests/test_learning_store.py::LearningStoreTests::test_list_pending_reviews_uses_stable_review_id_tie_breaker`，以逆序插入两个相同 `created_at` 的全局 review 和项目 review。

- 修复前，全局列表返回 `rev-global-b, rev-global-a`，证明结果受插入顺序影响。
- 修复后，全局列表稳定为 `rev-global-a, rev-global-b`，项目列表稳定为 `rev-project-a, rev-project-b`。

## 回归证据

- 学习存储、记忆运行时、记忆门专项：`83 passed`。
- `scripts/check.ps1`：后端 `1153 passed, 1 warning`，Python compileall 通过。
- 前端生产构建通过，7 组前端测试共 `48 passed`。
- Tauri `cargo check --locked` 通过。

## 边界

本轮只证明待审核列表的确定性排序，不证明记忆候选的语义正确性、审核决策质量、分页游标协议、跨进程 exactly-once、真实 UI/E2E 或答案质量提升。当前列表仍是一次性本地查询，没有新增后台队列或跨主机协调器。
