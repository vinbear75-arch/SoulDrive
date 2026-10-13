# 记忆来源会话跨投影闭合优化

日期：2026-09-15  
范围：反思接受、运行时记忆、加密 durable 归档、Redis 语义投影、PostgreSQL 关系投影和 memory outbox 重放

## 外部实践对照

- [Mem0 How It Works](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx) 将用户、项目、运行和其他 metadata 作为记忆范围的一部分，并把 SQL 事实源、向量检索层和实体层视为不同用途的存储；来源范围不能只停留在一个写入入口。
- [Letta MemFS](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md) 将记忆作为带稳定路径和版本历史的持久状态，编辑后仍能在其他投影中识别同一记忆对象。
- [Graphiti](https://github.com/getzep/graphiti) 将 episode、group 和有效时间用于来源/范围隔离；本地不引入 Neo4j，而是沿用现有 `source_session_id`、`source_trace` 和 `record_version`，把来源身份继续传到派生存储。

## 发现的真实缺口

LearningStore 主记忆已经保存 `source_session_id`，反思草稿也会把来源会话绑定到已确认 memory。但 `/reflection/reviews/{review_id}/decide` 的派生同步调用没有显式传入 `session_id`。旧的 `ProductionSessionStore.persist_memory()` / `revoke_memory()` 只在调用者显式传参时写入归档和 Redis/PostgreSQL 的会话字段：

- 加密 durable 归档没有 `session_id`；
- Redis 向量的 `session_id` metadata 为空；
- PostgreSQL memory projection 的 `session_id` 为空；
- 后续 outbox 或向量 rehydrate 只能从已经不完整的归档恢复。

这不是主记忆正文丢失，而是“主事实源有来源、派生投影没有来源”的可追溯断裂。它会影响来源审计、按来源重放和未来需要按会话筛选的语义投影。

## 失败优先验证

新增 `test_memory_projection_preserves_source_session_when_projection_call_omits_session`，模拟只在 memory payload 中存在 `source_session_id`、投影调用不传 `session_id` 的真实兼容调用。旧实现首先在 durable 归档断言处失败：

```text
KeyError: 'session_id'
```

随后补充了撤销路径和真实反思业务路径回归，确认 active upsert、revoke 和“反思草稿 → 接受 → durable 归档”都覆盖该边界。

## 最小实现

`ProductionSessionStore` 现在为两条记忆投影路径计算同一个规范化值：

```text
effective_session_id = explicit session_id or memory.source_session_id
```

显式调用参数优先；没有显式参数时从已验证的来源字段回退。该值同时写入：

- 加密文件归档，供断点恢复、向量 rehydrate 和 outbox 重放使用；
- Redis 向量 `session_id` metadata；
- PostgreSQL `souldrive_memory_records.session_id`；
- PostgreSQL/Redis outbox 事件载荷。

没有来源会话的旧兼容记忆仍保持无来源，不会猜测或生成虚假会话；没有新增数据库、向量服务、Agentic RAG 框架或自动记忆抽取器。

## 验证证据

- 跨投影来源回归：`tests/test_conversation_store.py` 相关 2 条通过。
- 会话/投影完整回归：`95 passed`。
- 真实反思来源路径：`test_reflection_draft_binds_source_session` 通过，并直接读取加密 durable 归档验证 `session_id`。
- 完整 `scripts/check.ps1`：后端 `1652 passed, 1 warning`；前端生产构建通过，UI 脚本合计 `66 passed`；Python `compileall` 与锁定 Cargo 检查通过。Vite 仍报告既有单 bundle 约 `587.48 kB` 警告。
- 冻结记忆评测：[memory_quality_after_memory_source_projection_20260915.json](../../eval/results/memory_quality_after_memory_source_projection_20260915.json)：`7/7` 场景、`8/8` 查询、Recall@K `1.0`、陈旧/越权泄漏率 `0.0`。
- 冻结 GraphRAG 路由：[graphrag_route_after_memory_source_projection_20260915.json](../../eval/results/graphrag_route_after_memory_source_projection_20260915.json)：`4/4`，路由/图门/信号契约均为 `1.0`。
- 研究窗口角色：[workflow_roles_after_memory_source_projection_20260915.json](../../eval/results/workflow_roles_after_memory_source_projection_20260915.json)：`6/6`，角色、就绪、检索、输出、引用、RAG 门和来源多样性均为 `1.0`。
- episodic runtime：`5/5`；baseline 保持 `2/5`，说明该评测仍在测量既有活动投影相对无投影基线的收益。
- 真实本地 GraphRAG acceptance：[graphrag_acceptance_after_memory_source_projection_20260915.json](../../eval/results/graphrag_acceptance_after_memory_source_projection_20260915.json)：`index_ready`、`community_target_ok`、`candidate_all_completed`、`candidate_no_refusal`、`candidate_all_cited`、`global_source_diversity_satisfied`、`global_route_enabled`、`local_route_enabled`、`ordinary_route_keeps_hybrid`、`project_index_ready` 全部为 `true`。

## 边界

本轮只证明本机单用户链路中来源会话可以从主记忆继续进入文件/Redis/PostgreSQL 投影和恢复载荷，不证明真实 Redis/PostgreSQL 连接、跨主机 exactly-once、跨存储断电原子性、向量召回率、模型答案准确率或真实 Tauri/WPS UI/E2E。来源字段缺失的历史归档不会被猜测修复，需要单独的数据迁移或人工确认。
