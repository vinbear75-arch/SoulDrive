# 记忆管理列表 Outbox 故障隔离优化

日期：2026-09-15  
范围：`GET /learning/memories`、memory outbox claim、SQLite 事实源列表

## 外部实践对照

- [LangChain 长期记忆](https://docs.langchain.com/oss/python/langchain/long-term-memory) 将跨会话记忆放在持久化 store 中，检索是对 store 的受控读取；本地管理列表继续以 LearningStore 的事实记录为准，不把派生投影当成唯一来源。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 将检查点/待写入状态与图执行恢复分开，失败步骤可从持久状态恢复；本轮沿用已有 pending outbox，不在一次列表请求中做无界重试。
- [Mem0 memory model](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx) 明确区分 SQL 事实与向量/实体派生存储，并要求读取按 scope 过滤；本轮只隔离派生 claim 异常，不放宽项目/global 范围。

## 发现的真实缺口

上一轮已让 `/learning/search` 在 Outbox claim 失败时继续读取事实源，但 `GET /learning/memories` 仍直接调用 `_replay_learning_memory_outbox()`。因此 SQLite 锁、临时连接故障或派生载荷解密异常会让管理列表返回 HTTP 500，即使同一 LearningStore 的事实记忆仍然可以正常列出。

## 失败优先验证

新增 `test_memory_list_keeps_fact_source_available_when_outbox_claim_fails`：创建真实临时工作区记忆，仅让 outbox replay 抛出临时锁异常，保留事实列表读取。旧实现真实返回 `500`，堆栈落在 `/learning/memories` 的 replay 调用处。

## 最小实现

`GET /learning/memories` 现在把 outbox replay 作为可失败的派生步骤：

1. 正常 claim/replay 时保持既有 project/global scope 和 `sync_replay` 结果；
2. claim 本身异常时记录服务端诊断日志，返回固定 `{"completed": 0, "failed": 1}`；
3. 继续使用同一 LearningStore 读取事实记忆、计算 validity status 和健康信息；
4. 返回固定的“记忆派生同步暂不可用，已使用本地事实源列表”提示，不泄露底层异常文本；
5. 保留 pending 事件，后续搜索、列表或 resume 入口仍可重试。

## 验证证据

- 失败优先：旧实现 Outbox claim 异常时 `500`；修复后事实列表仍为 `200`。
- Outbox/列表/搜索组合回归：`4 passed, 1 warning`。
- 学习模块完整回归：`134 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1656 passed, 1 warning`；前端生产构建通过，UI 脚本 `66 passed`；`compileall` 与锁定 Cargo 检查通过。既有 Vite bundle 约 `587.48 kB` 警告仍在。
- 冻结 memory 评测：[memory_quality_after_learning_memory_list_resilience_20260915.json](../../eval/results/memory_quality_after_learning_memory_list_resilience_20260915.json)：`7/7` 场景、`8/8` 查询，Recall@K `1.0`，陈旧/越权泄漏率 `0.0`。
- GraphRAG 路由：[graphrag_route_after_learning_memory_list_resilience_20260915.json](../../eval/results/graphrag_route_after_learning_memory_list_resilience_20260915.json)：`4/4`。
- 研究窗口角色：[workflow_roles_after_learning_memory_list_resilience_20260915.json](../../eval/results/workflow_roles_after_learning_memory_list_resilience_20260915.json)：`6/6`。
- Episodic workflow routing：runtime `5/5`，baseline `2/5`；本轮未修改活动投影。
- 真实本地 GraphRAG acceptance：[graphrag_acceptance_after_learning_memory_list_resilience_20260915.json](../../eval/results/graphrag_acceptance_after_learning_memory_list_resilience_20260915.json)：`index_ready`、`community_target_ok`、`candidate_all_completed`、`candidate_no_refusal`、`candidate_all_cited`、`global_source_diversity_satisfied`、`global_route_enabled`、`local_route_enabled`、`ordinary_route_keeps_hybrid`、`project_index_ready` 全部为 `true`。

## 边界

本轮证明的是本机单用户链路在记忆派生 outbox claim 暂时失败时仍能读取已提交事实源，并明确投影降级状态；不证明跨进程/跨主机 SQLite、真实 Redis/PostgreSQL、事务性 outbox、外部 exactly-once、长期记忆内容质量、模型答案准确率或真实 Tauri/WPS UI/E2E。若事实源本身不可读，接口仍应按真实存储错误失败，不能用本轮降级提示掩盖事实源故障。
