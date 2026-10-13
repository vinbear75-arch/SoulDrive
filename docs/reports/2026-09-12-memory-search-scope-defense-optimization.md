# SoulDrive 记忆搜索作用域防御性加固

日期：2026-09-12

## 本轮结论

本轮审计发现，SoulDrive 的 HTTP `/learning/search` 已经强制使用 `context_only=True`，但底层 `LearningStore.search()` 只有在该标志为真时才应用 `project_id` SQL 过滤。任何直接调用存储层、却传入项目 ID 但漏传 `context_only` 的路径，都会把其他项目的记忆带入结果。

## 外部实践对照

- [LangGraph long-term memory](https://langchain-ai.github.io/langgraph/how-tos/persistence-functional/) 使用 namespace 区分用户/应用记忆，并在 namespace 内搜索和写入；本轮将同样的“作用域是存储层约束”原则落到已有 `project_id` 过滤。
- [LangGraph semantic search deployment](https://langchain-ai.github.io/langgraph/cloud/deployment/semantic_search/) 明确把 namespace、metadata 和单一 embedding 索引配置作为记忆检索边界；本轮不引入语义存储，只修复关键词/历史版本查询的范围门。
- [Mem0 memory store](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) 对 `user_id`、`agent_id`、`run_id`、`actor_id` 等身份字段做不可被更新元数据覆盖的保护；本轮对应地把项目身份过滤下沉到 SQLite 查询，避免调用方漏传模式参数造成越界。
- [Letta memory schema](https://github.com/letta-ai/letta/blob/main/letta/schemas/memory.py) 将 core/archival/recall 作为可审计的不同记忆层；SoulDrive 仍保持现有 global/project、当前/历史版本和派生 chunk 的简单模型，不为小型本机项目增加层级。

## 失败优先证据

- 新增 `test_memory_search_project_scope_is_enforced_without_context_only_flag`：先复现 `project-a` 查询同时返回 `project-b` 的问题。
- 修复后该测试与 `tests/test_learning_store.py` 其余用例共 `68 passed`；项目/全局、时间有效性、版本历史和同步 outbox 相关用例保持通过。

## 最小实现

- `LearningStore.search()` 现在只要收到 `project_id`，就无条件附加项目/全局范围谓词；`include_global=False` 时仅保留当前项目。
- 当前记忆 chunk 与 `memory_versions` 历史查询都采用同一范围规则；`context_only` 仍只负责是否启用上下文时间/全局默认语义，不再承担项目隔离职责。
- 没有项目 ID 的显式全局搜索保持原有行为，兼容管理/诊断场景。

## 验证证据

- 记忆存储专项：`68 passed`。
- 仓库级 `scripts/check.ps1`：后端 `1181 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组前端测试共 `49 passed`；Tauri `cargo check --locked` 通过。
- 前端仍报告既有的大 chunk 警告；本轮未将其混入记忆质量结论。

## 明确边界

- 本轮修复的是本机单用户项目作用域越界风险，不是多租户 RBAC、跨主机一致性或分布式存储设计。
- 没有新增语义记忆模型、namespace 服务或依赖；现有语义投影仍沿用已有 scope 过滤与降级路径。
- `scripts/check.ps1` 不证明真实 Tauri UI、安装包启动或线上部署行为。
