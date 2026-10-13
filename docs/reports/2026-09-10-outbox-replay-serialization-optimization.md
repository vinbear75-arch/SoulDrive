# 会话 outbox 并发重放串行化优化记录

日期：2026-09-10

## 结论

本轮修复了本地会话派生同步的并发重放缺口。`ProductionSessionStore.replay_outbox()` 会从启动、上下文加载和历史加载等多个入口触发；原实现允许同一个 store 实例的两个线程同时读取同一 pending 事件，并在其完成标记写入前同时进入派发。现在同一实例内的 outbox 重放由互斥锁串行化，第二个调用会在第一个调用完成并写入状态后再重新读取 pending 集合。

## 外部实践对照

- [Redis Transactions](https://redis.io/docs/latest/develop/using-commands/transactions/) 将 `WATCH/MULTI/EXEC` 定义为乐观锁/CAS 语义，并指出检测到竞争后应以最新值重试；这说明“读取后再写入”不能默认视为并发安全。
- [Qdrant consistency guarantees](https://qdrant.tech/documentation/scaling/consistency-guarantees/) 明确提醒并发更新同一点可能在不同副本上呈现不同结果，并提供写入排序与一致性选项。SoulDrive 当前不是多副本服务，因此先落实更小的本进程串行化边界。
- [Temporal Activity execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx) 将重试视为可能再次执行的边界，外部副作用仍需幂等；本轮没有把互斥锁误称为 exactly-once，而是减少同一进程内的重复派发窗口。

## 失败优先验证

- 新增 `test_outbox_replay_is_serialized_per_session_store`：插入一个 pending 事件，让第一次派发阻塞，再并发启动第二次重放。
- 修复前第二个线程会在第一次完成前再次进入派发，回归失败；修复后只派发一次，事件被标记完成，第二次读取不到 pending 事件。
- `tests/test_conversation_store.py`：`38 passed`。
- 本轮完成时的 `scripts/check.ps1` 快照为后端 `1122 passed, 1 warning`；前端生产构建成功，7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。当前最新见 `docs/reports/2026-09-10-historical-memory-readonly-optimization.md`，为后端 `1123 passed, 1 warning`。

## 实现

- `ProductionSessionStore.__init__` 创建 `_outbox_replay_lock`。
- `replay_outbox` 只负责取得实例锁，再调用 `_replay_outbox_locked` 完成本地文件 outbox 和 PostgreSQL 控制面 outbox 的既有重放逻辑。
- 未改变事件格式、完成标记、失败保留 pending、退避策略或下游的 message-id/version 幂等门。

## 边界

- 这是同一个 Python 进程、同一个 `ProductionSessionStore` 实例内的互斥，不是跨进程、跨主机或 Redis/PostgreSQL/文件的分布式锁。
- 当前本机单用户 sidecar 边界不建设常驻消息队列；如果未来允许多个 sidecar/worker 共同消费同一 outbox，必须增加 SQLite claim/lease 或外部 CAS，并补进程崩溃、租约接管和时钟故障测试。
- 互斥不能替代下游幂等：进程可能在派生写入成功、完成标记尚未提交时崩溃，恢复仍可能重放一次；现有事件消费者的版本、message-id 和删除存在性门继续承担安全边界。
