# 记忆检索同分结果的确定性 tie-breaker 优化

日期：2026-09-12

## 结论

本轮修复了记忆读取 gate 在同分结果下依赖上游列表顺序的问题。此前两个记忆的相关性、类型和更新时间相同时，`max_items` 会先按输入顺序截取；关键词检索、语义检索合并或数据库返回顺序变化，都可能让同一个查询注入不同的记忆。

现在 `gate_memory_context` 和 `build_memory_capsule` 都把稳定的 `memory_id`/`asset_id` 作为最后 tie-breaker。这样不改变分数或业务优先级，只让同一输入集合在不同返回顺序下产生相同的 admitted ID 和胶囊顺序。

## 外部实践对照

- [LangGraph `_algo.py`](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/pregel/_algo.py) 在并行任务写入前按稳定路径排序，并明确将其作为确定性更新顺序；这对应 SoulDrive 在读取门处不能把上游返回顺序当作业务优先级。
- [LangGraph checkpointer 文档](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx) 的 SQL 示例使用可排序的 `checkpoint_id`，并在同分/同时间场景中依赖稳定主键保证最新状态和历史读取可复现。
- [Graphiti 关于 episode 排序的 issue](https://github.com/getzep/graphiti/issues/1724) 明确提出在时间排序后追加 UUID tie-breaker，避免同时间结果顺序不稳定；本轮只迁移这个排序原则，没有改变 SoulDrive 的检索策略或引入 Graphiti。

## 失败优先验证

- 新增 `MemoryRuntimeTests.test_gate_memory_context_uses_stable_memory_id_tie_breaker`。
- 修复前：两个 `project_fact` 的分数均为 `0.5`、更新时间均为 `100`，`max_items=1` 时，反转输入列表会把 admitted ID 从 `mem-a` 变为 `mem-b`，测试失败。
- 修复后：正序和逆序输入都只接受 `mem-a`；已有胶囊排序也使用同一稳定 ID 规则。

## 实现与验证

- 修改 `core/memory_runtime.py` 的 gate 和 capsule 排序键，增加 `memory_id`/`asset_id` 作为稳定末级排序字段。
- 记忆/RAG/学习组合回归：`246 passed, 1 warning`。
- `scripts/check.ps1`：后端 `1145 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组测试共 `48 passed`；Tauri `cargo check --locked` 通过。
- 警告为环境中的 Starlette/httpx 弃用提示，不是测试失败。

## 边界

- 这是读取顺序和评测复现性修补，不证明检索召回、引用正确率或答案准确率提升。
- 只有同等业务排序键完全相同时才使用 ID tie-breaker；不会覆盖项目范围、人工确认、版本、新鲜度和相关性裁决。
- 该改动不等于跨存储全局排序协议；如果未来引入分布式检索器，需要继续定义统一分数、分页游标和跨 shard tie-breaker。
