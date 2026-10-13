# 运行时记忆 receipt 的派生投影恢复

## 外部机制与本地缺口

[Temporal Workflow replay](https://docs.temporal.io/workflows)把持久 Event History 作为恢复依据，已完成 Activity 的结果在重放时复用；[Restate 的 HTTP invocation idempotency](https://docs.restate.dev/services/invocation/http)也把稳定调用身份与幂等回放作为 durable execution 的边界。SoulDrive 当前仍是本机 SQLite/加密归档，不引入这些运行时，但需要让同一个 runtime memory receipt 在“事实已提交、派生投影暂时失败”时具备可恢复语义。

此前 `/learning/memories/runtime` 首次保存成功但 `_persist_accepted_learning_assets()` 返回 `degraded` 时，会留下 pending `learning_sync_outbox`；同一 `request_id` 重试只验证 active canonical memory 后直接返回 `saved`，不会推进 pending projection。用户若不另行访问记忆列表，幂等重试无法完成这次 durable work。

## B0 反例

新增反例让第一次派生归档返回 `degraded`、第二次返回 `ok`，随后重复同一个 `request_id`。旧实现第一次返回 degraded，第二次虽然返回 idempotent saved，但派生调用次数仍为 1，`memory_sync_outbox` 仍 pending；测试结果为 `1 failed, 152 deselected, 1 warning`。

## 最小修复

- completed runtime receipt 仍先验证原 reflection review、accepted candidate、active canonical memory 和 project/global scope，不重新创建或接受 memory。
- 只有验证通过后，在同一个已打开的 LearningStore 上调用已有 `_replay_learning_memory_outbox()`，限制 20 条并按当前项目加 global scope 回放；没有 pending 事件时为零开销摘要。
- 返回 `memory_sync_replay: {completed, failed}`，让客户端区分“事实结果已回放”和“派生同步仍需重试”；恢复异常不把已存在的 canonical fact 伪装成未保存。
- 保留已有 receipt 输入摘要与 memory identity 校验，不扩大为跨主机锁或 exactly-once 声明。

## 验收证据

- B0：`1 failed, 152 deselected, 1 warning`。
- 修复后运行时记忆回归：`12 passed, 1 warning`；运行时记忆/outbox/storage sync 组合：`22 passed, 1 warning`。
- 第四十一轮第二次独立完整 `scripts/check.ps1`：退出码 `0`；后端 **1772 passed、1 warning，439.17 秒**；记忆质量 **7/7 场景、8/8 查询、Recall@K=1.0、陈旧/禁止泄漏率=0.0**；GraphRAG 路由 **4/4**；研究窗口角色 **6/6**；episodic runtime **5/5**；前端生产构建成功、UI **66 passed**；`compileall` 与锁定 Cargo 检查通过。

## 仍未证明

- 这是本机事实源到本机派生归档的有界恢复，不是跨进程/跨主机、真实 Redis/PostgreSQL、断电恢复或外部副作用 exactly-once 证明。
- 回放最多处理 20 个当前 scope 的 pending 事件；失败仍可观察并留在 outbox，不能宣称一次调用完成整个 backlog。
- 没有因此证明 Agentic RAG、长期记忆开放式答案质量、LongMemEval 分数、GraphRAG 内容收益或真实 UI/E2E。
