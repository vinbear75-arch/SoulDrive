# 未绑定项目的历史记忆范围隔离优化

日期：2026-09-12

## 公开实践对照

- [LangGraph long-term memory](https://docs.langchain.com/oss/python/langchain/long-term-memory) 将长期记忆放在显式 namespace，并把跨会话检索建立在 namespace/过滤语义上。
- [LangGraph memory concepts](https://docs.langchain.com/oss/python/concepts/memory) 区分 thread-scoped 短期状态与跨会话长期记忆；持久化不等于可以无范围读取所有记忆。
- [Mem0 memory filters](https://docs.mem0.ai/platform/features/v2-memory-filters) 将用户、agent、时间和元数据过滤作为记忆检索条件；[Mem0 delete memory](https://docs.mem0.ai/core-concepts/memory-operations/delete) 对无过滤的批量删除要求显式确认，体现了默认收窄、扩大范围必须显式表达的原则。

本地实现继续使用加密 SQLite、`project_id` 和 `scope`，没有引入外部记忆服务；借鉴的是命名空间和时间过滤的边界语义。

## 失败优先发现

运行时 `LearningStore.search(..., context_only=True)` 已经只读取全局或当前项目，但历史查询路径单独传入 `as_of` 时，若调用方没有传 `project_id`，旧 SQL 会读取所有项目的活动 chunk 和 `memory_versions`。`list_memories(as_of=...)` 也会返回项目记忆。

新增回归 `test_unbound_historical_memory_reads_are_global_only`：同一查询词写入一条全局事实和一条 `project-a` 事实，未绑定项目执行 `search(..., as_of=...)` 与 `list_memories(as_of=...)`。旧实现的 search 结果同时包含“项目历史事实”和“全局历史事实”。

## 最小修复

- `LearningStore.search()`：无 `project_id` 的历史查询把当前记忆 chunk 和历史版本限制为 `scope = 'global'`。
- `LearningStore.list_memories()`：无 `project_id` 的 `as_of` 历史列表只读取全局 head 与全局历史版本。
- 保留无 `as_of` 的管理列表行为，因为管理层需要在已确认的项目上下文中编辑定时/过期项目记忆；HTTP 管理端点本身仍以 `context_only` 强制无项目时 global-only。

## 验证结果

- 新增历史范围回归：通过。
- 学习存储与学习 API 组合：`140 passed, 1 warning`。
- 完整 `scripts/check.ps1` 已通过：后端 `1190 passed, 1 warning`；前端构建通过，7 组前端测试全部通过；Tauri `cargo check --locked` 通过。

## 边界

- 这项修复只证明未绑定项目的历史读取不会混入项目记忆；不证明记忆内容抽取、冲突裁决、语义向量召回或事实正确性。
- 无项目的管理列表仍保留兼容行为，依赖上层管理 API 的 `context_only` 和项目编辑门；这不是把管理查询宣称为运行时上下文。
- 历史查询仍只使用本地记录的 `valid_from`/`invalid_at`，不推断用户没有提供的真实发生时间。
