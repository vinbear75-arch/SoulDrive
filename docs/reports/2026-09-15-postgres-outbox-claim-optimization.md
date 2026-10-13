# PostgreSQL outbox 并发 claim 优化

日期：2026-09-15  
范围：可选 PostgreSQL 控制面 outbox 的跨 worker replay

## 外部实践与可复现缺口

对照 [PostgreSQL SELECT locking](https://www.postgresql.org/docs/current/sql-select.html) 与 [PostgreSQL explicit locking](https://www.postgresql.org/docs/17/explicit-locking.html)，`FOR UPDATE SKIP LOCKED` 适合队列消费者：已被其他事务锁住的行会被跳过，避免多个消费者同时拿到同一条待处理记录。它解决的是并发 claim，不是外部服务 exactly-once。

SoulDrive 原来的 PostgreSQL replay 只执行 `SELECT ... WHERE status = 'pending' LIMIT ...`。两个独立 worker 可以同时读到同一事件，随后都执行 `_dispatch_event()`；进程内 replay 锁不能覆盖两个进程，也不能覆盖两个连接。

## 失败优先与最小实现

- `souldrive_outbox_events` 增加 `claim_token` 与 `claim_until`，并建立 claim 查询索引。
- `claim_pending_outbox()` 用单条 `WITH ... FOR UPDATE SKIP LOCKED -> UPDATE ... RETURNING` 原子领取 pending 事件，并可重领已过期的 `in_flight` 事件。
- 成功/失败回执携带 claim token；不匹配的迟到 worker 不能覆盖新 worker 的状态。
- `mark_outbox()` 现在返回实际更新行数，replay 只有在 token 回执确实提交时才计入 `completed`；claim 已丢失时进入失败统计，避免把未确认事件伪报为完成。
- replay 仍把外部副作用定义为 at-least-once：worker 可能在 dispatch 成功、回执提交前崩溃，后续 lease 恢复会再次 dispatch。没有把它包装成 exactly-once。

## 回归与门禁

- 失败优先：两个独立 `ProductionSessionStore` worker 对同一个共享 PostgreSQL outbox 测试替身并发 replay；旧链路 dispatch 次数为 `2`，修复后为 `1`，且 completion 携带 claim token。
- 失败优先：completion 回执返回 `rowcount=0` 时，旧 replay 会伪报 `completed=1`；修复后结果为 `completed=0, failed=1`。
- 会话/outbox 定向回归：`88 passed`。
- 本轮完整工程门禁：后端 `1577 passed, 1 warning`（约 347 秒）；前端生产构建成功、UI 脚本 `62 passed`；Python `compileall` 与锁定 Cargo 通过。

## 边界

本轮用可控替身验证应用层 claim/replay 协议；本机没有真实 PostgreSQL 服务，未宣称真实驱动、事务隔离、连接池、故障恢复窗口或外部 Redis/SMTP/日历 exactly-once 已验收。SQLite 本地 outbox 仍由现有单进程 replay 锁和追加快照机制负责，不能被本轮 PostgreSQL claim 结论替代。
