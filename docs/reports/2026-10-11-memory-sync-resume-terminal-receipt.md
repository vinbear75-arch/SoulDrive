# 2026-10-11 记忆同步恢复终态幂等回执

日期：2026-10-11；第三十三轮；状态：Candidate 已实现并通过完整工程门禁。

## 范围

本轮只检查 `POST /learning/memory-sync/{event_id}/resume` 的幂等键是否绑定完整的业务结果。此前成功恢复会保存 durable receipt，但“事件当前尚未暂停，因此本次没有恢复”只返回临时结果；如果事件随后进入 paused，同一请求键重试会被当成新的恢复请求执行。

本轮没有修改冻结集、gold、训练/调参配置、索引、模型或外部后端，没有改变不存在事件的查找语义。

## 外部机制对照

- [Restate HTTP invocation idempotency](https://docs.restate.dev/services/invocation/http) 将稳定请求键与完成响应绑定，重复请求应回放同一业务结果。
- [DBOS workflow IDs and idempotency](https://docs.dbos.dev/python/tutorials/workflow-tutorial) 用稳定业务身份避免同一请求在状态变化后被重新启动。
- [Temporal Workflow ID / Run ID](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-execution/workflowid-runid.mdx) 区分稳定业务身份和执行尝试；本地继续使用 `event_id + project_id`，不把 claim token 当作客户端幂等键。

本地仍采用最小方案：复用已有加密 `memory_mutation_receipts`，不引入工作流引擎。

## B0 失败优先

新增两个反例前，旧实现表现为：

1. 对一个尚未暂停的事件首次使用幂等键，得到 `resumed=false`，但没有写回执；
2. 让同一事件后来达到失败上限进入 paused，再用同一键重试，旧实现会执行恢复并返回 `resumed=true`。

失败优先测试结果：`1 failed, 101 deselected`。这证明缺口是状态变化后的请求语义漂移，而不是单纯的 HTTP 状态码问题。

## 最小实现

- `LearningStore.resume_memory_sync()` 在已有事件处于 `pending + retry_paused_at IS NULL` 时，把 `resumed=false` 的终态响应写入同一加密 receipt。
- receipt 回放保留已存储的 `resumed` 值，不再无条件把所有回放改写成 `true`。
- API 在携带幂等键的 `409 not_paused` 中返回 `idempotent_replay=false/true`，让客户端区分首次终态和同键回放。
- 事件不存在仍不写入终态回执，避免把一次查找竞态固化成未来事件的永久结果；项目范围冲突仍 fail-closed。
- 首次真正恢复、同键成功回放和换输入冲突的既有语义保持不变。

## 验证证据

- B0 反例：`1 failed, 101 deselected`。
- 记忆同步存储/API 定向回归：`250 passed, 1 warning`。
- `python scripts/validate_business_seed_review.py`：30 条待审核种子、4 类任务分布、7 个声明来源指纹通过；仍为 `pending_review`、`scoring_performed=false`、`ready_for_freeze=false`。
- 完整 `scripts/check.ps1`：退出码 `0`；后端 `1760 passed, 1 warning，419.39 秒`；记忆质量 `7/7` 场景、`8/8` 查询、Recall@K `1.0`、陈旧/越权泄漏率 `0.0`；GraphRAG 路由 `4/4`；研究窗口角色 `6/6`；前端构建成功；UI 脚本 `66 passed`；锁定 Cargo 检查通过。
- 当前变更文件 SHA-256：
  - `core/learning_store.py` = `809B5E5B62D03B0E1CCB92603D7DA16740277106812A26C156D738D37DDEC854`
  - `core/mcp_server.py` = `1278327293A5BB90E14A3817715CDC786BB82D98E8DCE17ABD4A0334E1449EA0`
  - `tests/test_learning_store.py` = `519842EAFDF6CED6080DA17F7D193C6B8FDE9DE421E594F1E92CA14BB5890C02`
  - `tests/test_mcp_server_learning.py` = `90CF1B92A23EA90296513D52FC4B955A207D7B6BE537EFA5F2DF457208819AA4`

## 结论与边界

本轮证明的是本机单用户 SQLite 事务内，恢复请求的终态结果不会因 outbox 后续状态变化而被同键重试改写；同时成功恢复仍可在事件完成后回放。它不证明断电测试、跨进程/跨主机 receipt、真实 Redis/PostgreSQL、外部 exactly-once、下游投影完成、开放式记忆答案质量或真实 Tauri/WPS UI/E2E。

既有 Starlette/httpx 弃用警告和 Vite 单 chunk 体积警告仍为非阻断项。下一轮继续审计其他 durable receipt 的 schema/version、失败恢复和旧事件兼容路径，不把本轮局部通过写成项目全部完成。
