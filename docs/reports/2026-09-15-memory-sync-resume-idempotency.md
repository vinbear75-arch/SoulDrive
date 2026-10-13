# 记忆派生同步人工恢复幂等性优化

日期：2026-09-15  
范围：`POST /learning/memory-sync/{event_id}/resume`、Idempotency-Key、memory sync durable receipt

## 外部实践对照

- [Restate HTTP invocation idempotency](https://docs.restate.dev/services/invocation/http) 对同一幂等键保留完成响应，重复调用返回相同结果而不重新执行处理器；本地恢复接口采用同样的“稳定请求身份 + durable response replay”边界。
- [DBOS workflow IDs and idempotency](https://docs.dbos.dev/python/tutorials/workflow-tutorial) 使用稳定 workflow ID 防止同一业务执行重复启动，并从已完成步骤恢复；本地以 `event_id + project_id` 作为恢复输入，把恢复结果写入已有加密 receipt。
- [Temporal Workflow ID / Run ID](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-execution/workflowid-runid.mdx) 区分稳定业务 Workflow ID 与可变化 Run ID；本地不引入工作流引擎，只把 Outbox `event_id` 作为稳定业务对象标识，不把 claim token 当作客户端幂等键。

## 发现的真实缺口

人工恢复接口原先没有 `Idempotency-Key`。如果恢复 SQL 已提交、但客户端在收到响应前断线，重试会再次看到事件已经不是 paused，并收到 `409 not_paused`；客户端无法判断“恢复已经成功”还是“第一次请求没有执行”。这会把 durable recovery 的正常网络重试变成人工二次判断，也没有稳定的完成响应回放。

## 失败优先验证

新增 `test_paused_memory_sync_resume_replays_same_idempotency_key`：真实创建项目记忆，连续达到 `MEMORY_SYNC_MAX_ATTEMPTS` 进入 paused；旧实现即使携带同一请求键也不返回 `idempotent_replay`，重复恢复沿用 `not_paused` 分支。测试还在首次恢复后将 Outbox 事件标为 completed，再验证重试必须依赖 durable receipt，而不能依赖事件仍是 pending。

## 最小实现

- `LearningStore.resume_memory_sync()` 接受可选幂等键，使用现有加密 `memory_mutation_receipts` 表保存 `event_id + project_id` 输入摘要和恢复结果，不新增重量级 workflow runtime。
- 首次恢复在同一 SQLite 事务中同时清除暂停状态、重置尝试次数并写入 receipt；写入失败时整个事务回滚。
- 同键重试先回放 durable receipt，即使 Outbox 事件之后已进入 completed 也仍返回同一业务结果；同键换事件/项目因输入摘要变化返回冲突。
- 接口公开 `Idempotent-Replay` 结果标志；首次恢复仍只写一次 `learning.memory_sync_resumed` 审计事件，回放不重复写审计。
- 无幂等键的旧调用协议和项目范围校验保持不变；未暂停或不存在的事件不会被伪造为成功。

## 验证证据

- 失败优先：旧实现对恢复接口不返回 `idempotent_replay`，同键重试无法得到稳定成功回执；修复后首次 `200/idempotent_replay=false`，事件完成后同键仍 `200/idempotent_replay=true`，换项目同键 `409`。
- 恢复与项目范围定向回归：`2 passed, 1 warning`；新增完成后回放场景单独复测 `1 passed, 1 warning`。
- LearningStore memory sync/idempotency 相关：`14 passed`。
- 学习模块完整回归：`135 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1657 passed, 1 warning`；前端生产构建通过，UI 脚本 `66 passed`；`compileall` 与锁定 Cargo 检查通过。既有 Vite bundle 约 `587.48 kB` 警告仍在。
- 冻结 memory 评测：[memory_quality_after_memory_sync_resume_idempotency_20260915.json](../../eval/results/memory_quality_after_memory_sync_resume_idempotency_20260915.json)：`7/7` 场景、`8/8` 查询，Recall@K `1.0`，陈旧/越权泄漏率 `0.0`。
- GraphRAG 路由：[graphrag_route_after_memory_sync_resume_idempotency_20260915.json](../../eval/results/graphrag_route_after_memory_sync_resume_idempotency_20260915.json)：`4/4`。
- 研究窗口角色：[workflow_roles_after_memory_sync_resume_idempotency_20260915.json](../../eval/results/workflow_roles_after_memory_sync_resume_idempotency_20260915.json)：`6/6`。
- Episodic workflow routing：runtime `5/5`，baseline `2/5`；本轮未修改活动投影。
- 真实本地 GraphRAG acceptance：[graphrag_acceptance_after_memory_sync_resume_idempotency_20260915.json](../../eval/results/graphrag_acceptance_after_memory_sync_resume_idempotency_20260915.json)：`index_ready`、`community_target_ok`、`candidate_all_completed`、`candidate_no_refusal`、`candidate_all_cited`、`global_source_diversity_satisfied`、`global_route_enabled`、`local_route_enabled`、`ordinary_route_keeps_hybrid`、`project_index_ready` 全部为 `true`。

## 边界

本轮证明的是本机单用户 SQLite 事务内的恢复请求幂等回放，不证明跨进程/跨主机统一 receipt、真实 Redis/PostgreSQL、外部系统 exactly-once、审计 sink 事务一致性或模型答案质量。receipt 只保证 SoulDrive 自身恢复状态的重复请求不会重新执行恢复 SQL；它不能证明下游已经完成外部向量/文件副作用。
