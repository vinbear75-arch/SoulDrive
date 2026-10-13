# 记忆 outbox 完成回执确认优化

日期：2026-09-12

## 结论

本轮修复了记忆派生同步回放的成功计数缺口。此前 `_replay_learning_memory_outbox` 只要下游文件/向量投影返回 `status=ok`，就忽略 `complete_memory_sync(...)` 的布尔结果并计入 `completed`。如果当前工作者的 claim 已过期、被其他工作者接管或完成标记没有写入，该事件仍会被报告为成功，造成 durable replay 的观测与真实状态不一致。

现在只有完成标记明确写入成功时才计入 `completed`；完成回执未确认时计入 `failed`，不覆盖 pending 事件，后续由租约/退避机制继续回放。下游投影仍沿用现有版本化、可重复执行路径，没有引入新的 worker 或数据库表。

## 外部实践对照

- [Temporal activity state](https://github.com/temporalio/temporal/blob/main/chasm/lib/activity/proto/v1/activity_state.proto) 为当前 attempt 保存 `stamp`/`started_stamp`，并将 activity 状态区分为 scheduled、started、completed、failed；旧 attempt 的任务不能仅凭下游动作冒充当前完成。
- [Temporal activity execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx) 将异步完成建模为“外部工作完成后再显式 complete”，说明下游动作结束与 durable execution terminal completion 是两个状态。
- [Microservices transactional outbox pattern](https://microservices.io/i/MicroservicePatternLanguage.pdf) 的核心边界是事件先可靠落盘，再由消费者处理；因此消费者成功必须包含可观测的确认状态，不能只根据副作用调用返回值报告完成。

本轮只采用“完成回执必须被确认”的语义，不把 Temporal/Restate 引入本机单用户项目。

## 失败优先验证

- 新增 `McpServerLearningTests.test_memory_sync_outbox_does_not_count_unconfirmed_completion_as_completed`。
- 在旧实现中，模拟下游投影成功但 `complete_memory_sync` 返回 `False`，回放结果为 `{'completed': 1, 'failed': 0}`，而事件仍然 pending；该行为与 durable 状态矛盾。
- 修复后同一反例返回 `{'completed': 0, 'failed': 1}`，事件保持 pending；既有“完成标记抛异常后可重试”回归仍通过。

## 实现与验证

- `core/mcp_server.py` 读取 `complete_memory_sync` 的布尔回执，只有 `True` 才增加 `completed`。
- 没有变更 outbox schema、claim token、租约、退避或下游投影协议。
- 目标回归：`2 passed, 1 warning`。
- 学习存储 + MCP 学习组合：`133 passed, 1 warning`。

## 边界

- `failed` 表示本次回放未确认完成，不等于下游副作用一定没有发生；因此系统保留事件以支持至少一次重放，投影路径必须继续保持幂等/版本安全。
- 如果另一个工作者已经完成事件，本次工作者可能仍将“未能确认自己的 completion”计入 `failed`；这是本次回放视角的保守计数，不会把事件重新写成 pending。
- 本轮未实现跨进程全局协调器、心跳、可恢复进程快照或 exactly-once 外部副作用；真实 durable execution 仍需以当前本地 outbox/claim/退避边界为准。
