# 记忆搜索 Outbox 故障隔离优化

日期：2026-09-15  
范围：`/learning/search`、memory outbox claim、SQLite 事实源降级读取

## 外部实践对照

- [Dapr transactional outbox](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/) 把事件投递放在事务事实之后，由后台消费者验证、发布并在失败时重试；这说明投影消费者失败不应回写或伪造业务事实状态。本地沿用已有加密 SQLite outbox，不引入 Dapr broker。
- [Restate HTTP idempotency](https://docs.restate.dev/services/invocation/http) 通过幂等键保留请求结果、避免重试重复执行；本地 memory mutation 已有自己的 durable receipt，本轮只补查询侧的故障隔离，不把一次搜索包装成新的 durable workflow。
- [Restate request lifecycle](https://docs.restate.dev/guides/request-lifecycle) 将请求持久化、步骤日志和失败重放分开；对应到本地链路，LearningStore 是事实源，outbox/Redis 是可重建派生步骤，claim 失败不能让事实源读取整体失效。

## 发现的真实缺口

上一轮让 `/learning/search` 在正常情况下先消费 scoped memory outbox，但仍存在更窄的故障旁路：`claim_pending_memory_syncs()` 自身遇到 SQLite 锁、连接暂不可用或解密/读取异常时会抛出异常，搜索接口直接返回 500。

此时主 LearningStore 事实源仍然可能可以完成关键词检索；500 会把“派生投影恢复暂时失败”错误放大成“记忆不可用”，也不符合聊天上下文对派生投影降级的既有边界。

## 失败优先验证

新增 `test_learning_search_keeps_fact_source_available_when_outbox_claim_fails`，让 fake store 的 `claim_pending_memory_syncs()` 抛出临时锁异常，但 `search()` 仍返回已提交的 global memory。旧实现真实返回 `500`，堆栈落在 `/learning/search` 的 outbox replay 调用处。

## 最小实现

`/learning/search` 现在将 outbox claim 视为可选派生步骤：

1. claim/replay 成功时照常按 project/global scope 推进 pending 事件；
2. claim 本身异常时记录本地诊断日志，固定返回 `memory_sync_replay: {completed: 0, failed: 1}`；
3. 继续执行事实源关键词检索与原有 memory read gate；
4. 返回固定的“记忆派生同步暂不可用，已使用本地事实源检索”提示，不向客户端暴露底层异常文本；
5. 若审计 sink 同时失败，将两个 bounded warning 合并，不覆盖业务检索结果。

本轮没有放宽项目/工作流范围，没有把不一致的 Redis 结果当作权威，也没有自动重试到无界循环；下一次查询或既有 resume 入口仍可推进 pending 事件。

## 验证证据

- 失败优先：旧实现 claim 异常时 `500`；修复后事实源结果仍为 `200`。
- 搜索 outbox/审计故障组合：`3 passed, 1 warning`。
- 学习模块完整回归：`133 passed, 1 warning`。
- TaskJournal 完整回归：`29 passed`。
- 第二次完整 `scripts/check.ps1`：后端 `1655 passed, 1 warning`；前端生产构建通过，UI 脚本 `66 passed`；`compileall` 与锁定 Cargo 检查通过。既有 Vite bundle 约 `587.48 kB` 警告仍在。
- 冻结 memory 评测：[memory_quality_after_learning_search_outbox_resilience_20260915.json](../../eval/results/memory_quality_after_learning_search_outbox_resilience_20260915.json)：`7/7` 场景、`8/8` 查询，Recall@K `1.0`，陈旧/越权泄漏率 `0.0`。
- GraphRAG 路由：[graphrag_route_after_learning_search_outbox_resilience_20260915.json](../../eval/results/graphrag_route_after_learning_search_outbox_resilience_20260915.json)：`4/4`，路由/图开关/信号契约均为 `1.0`。
- 研究窗口角色：[workflow_roles_after_learning_search_outbox_resilience_20260915.json](../../eval/results/workflow_roles_after_learning_search_outbox_resilience_20260915.json)：`6/6`，窗口角色、就绪门、检索 profile、输出、引用、RAG 和来源多样性均为 `1.0`。
- Episodic workflow routing：runtime `5/5`，baseline `2/5`，本轮未修改活动投影。
- 真实本地 GraphRAG acceptance：[graphrag_acceptance_after_learning_search_outbox_resilience_20260915.json](../../eval/results/graphrag_acceptance_after_learning_search_outbox_resilience_20260915.json)：`index_ready`、`community_target_ok`、`candidate_all_completed`、`candidate_no_refusal`、`candidate_all_cited`、`global_source_diversity_satisfied`、`global_route_enabled`、`local_route_enabled`、`ordinary_route_keeps_hybrid`、`project_index_ready` 全部为 `true`。

## 边界

本轮证明的是本机单用户链路在派生 outbox claim 暂时失败时仍能读取已提交事实源，并明确投影降级状态；不证明 SQLite 跨进程/跨主机可用性、真实 Redis/PostgreSQL、事务性 outbox、外部 exactly-once、长期记忆内容质量或模型答案准确率。若事实源本身不可读，接口仍应按真实存储错误失败，不能用本轮降级提示掩盖事实源故障。
