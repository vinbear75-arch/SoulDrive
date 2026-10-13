# 记忆搜索入口的 Outbox 回放一致性优化

日期：2026-09-15  
范围：`/learning/search`、学习记忆 outbox、语义派生投影可见性

## 外部实践对照

- [Dapr transactional outbox](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/) 将业务事实提交与事件意图绑定，并由后台消费者在失败后重试；本地实现沿用已有 SQLite 事实源和加密 outbox，不引入 Dapr 或消息中间件。
- [Restate durable execution](https://docs.restate.dev/foundations/key-concepts) 将已记录的步骤在重试时跳过并从 journal 恢复，同时通过幂等键去重；本轮只借鉴“查询入口先推进待恢复步骤、失败不覆盖事实源”的边界，不宣称本机 outbox 已具备 Restate 的跨服务 exactly-once 能力。
- [LangGraph stores](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/stores.mdx) 将长期记忆按 namespace 隔离，并把持久 store 与语义搜索作为不同层；这与 SoulDrive 的 LearningStore 事实源、Redis 向量派生层、项目/工作流 scope 约束一致。

## 发现的真实缺口

记忆管理接口和聊天上下文入口已经在读取前调用 `_replay_learning_memory_outbox()`，但公开的 `/learning/search` 旧实现直接执行关键词/语义检索。于是会出现：

1. LearningStore 已经提交了新记忆，但其 Redis/其他派生投影事件仍处于 pending；
2. 聊天上下文会先推进事件，而独立搜索接口不会；
3. 同一个查询从两个入口得到不同的语义投影可见性，尤其当关键词没有命中而只能依赖向量投影时更明显。

这不是把 SQLite 事实源交给 Redis 的问题：关键词结果仍以 SQLite 为准；缺口是搜索入口没有执行已有的可重试投影恢复步骤。

## 失败优先验证

新增 `test_learning_search_replays_learning_outbox_before_retrieval`，先创建已接受但尚未完成派生同步的记忆，再调用 `/learning/search`。旧实现的真实结果是：

```text
persist_sync.call_count == 0
```

说明搜索入口没有消费 pending outbox。修复后，同一回归确认同步函数只调用一次、回放摘要为 `{"completed": 1, "failed": 0}`，且 pending 事件已清空。

## 最小实现

`/learning/search` 现在：

1. 先根据 `project_id` 计算现有 scope policy；
2. 仅在 store 提供 `claim_pending_memory_syncs` 时执行已有 scoped outbox replay，避免破坏只实现检索协议的测试/兼容 store；
3. 回放失败只记录 bounded warning，不让已提交的 SQLite 事实不可搜索；
4. 在响应中返回 `memory_sync_replay` 的 `completed/failed` 摘要，便于诊断派生投影延迟。

项目、global memory 和工作流 scope 仍由原有 `claim_pending_memory_syncs` / `LearningStore.search` / `gate_memory_context` 共同约束；没有扩大跨项目可见范围，也没有在搜索请求中新增后台线程或 Agentic RAG。

## 验证证据

- 失败优先：旧实现未调用派生同步；修复后新增搜索入口回归通过。
- 搜索与聊天 outbox 组合：`2 passed, 1 warning`。
- 学习模块完整回归：`132 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1654 passed, 1 warning`；前端生产构建通过，UI 脚本 `66 passed`；`compileall` 与锁定 Cargo 检查通过。既有 Vite bundle 约 `587.48 kB` 警告仍在。
- 冻结 memory 评测：[memory_quality_after_learning_search_outbox_20260915.json](../../eval/results/memory_quality_after_learning_search_outbox_20260915.json)：`7/7` 场景、`8/8` 查询，Recall@K `1.0`，陈旧/越权泄漏率 `0.0`。
- GraphRAG 路由：[graphrag_route_after_learning_search_outbox_20260915.json](../../eval/results/graphrag_route_after_learning_search_outbox_20260915.json)：`4/4`，路由、图开关和信号契约均为 `1.0`。
- 研究窗口角色：[workflow_roles_after_learning_search_outbox_20260915.json](../../eval/results/workflow_roles_after_learning_search_outbox_20260915.json)：`6/6`，角色、就绪、检索、输出、引用、RAG 和来源多样性均为 `1.0`。
- Episodic workflow routing：runtime `5/5`，baseline `2/5`；本轮未改变活动投影或 baseline 定义。
- 真实本地 GraphRAG acceptance：[graphrag_acceptance_after_learning_search_outbox_20260915.json](../../eval/results/graphrag_acceptance_after_learning_search_outbox_20260915.json)：`index_ready`、`community_target_ok`、`candidate_all_completed`、`candidate_no_refusal`、`candidate_all_cited`、`global_source_diversity_satisfied`、`global_route_enabled`、`local_route_enabled`、`ordinary_route_keeps_hybrid`、`project_index_ready` 全部为 `true`。

## 边界

本轮证明的是本机单用户链路中，搜索入口与聊天上下文入口共享已有的 scoped outbox 恢复步骤；不证明跨存储事务性 outbox、真实 Redis/PostgreSQL 可用性、跨进程/跨主机 exactly-once、语义召回率或模型答案准确率。派生同步达到最大重试后仍需通过既有 resume 接口和人工确认恢复；搜索不会静默重写或删除事实源。
