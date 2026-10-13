# 2026-09-13 索引任务幂等收据原子 upsert 优化

## 目标

修复索引任务 durable receipt 的并发竞态：同一 `Idempotency-Key` 的两个重试请求必须共享一个 `run_id`，不能因为同时执行“查询后插入”而让竞争请求变成唯一键 500。

## 外部实践对齐

- [issuance-gateway 的 SQLite 幂等设计](https://github.com/lemmebee/issuance-gateway/blob/master/docs/ARCHITECTURE.md)：用唯一约束和原子 `INSERT ... ON CONFLICT ...` 关闭 check-then-insert 竞态，竞争请求再回读同一记录。
- [idempotency-rs](https://github.com/bhatti/idempotency-rs)：把请求参数摘要绑定到幂等键，同键换输入必须拒绝，而不是静默复用旧结果。
- [sqlite-durable-workflow](https://github.com/eatdrop/sqlite-durable-workflow)：单机 durable job 仍需持久状态和 at-least-once 语义，不能把进程内锁当作崩溃后的幂等保证。

## 实际改动

`IndexRunJournal.create_or_get` 改为对 `idempotency_key` 使用 SQLite 原子冲突处理，再在同一事务中回读收据；回读后继续校验 `input_digest`，同键换输入仍返回 `IndexRunInputConflict`。没有引入全局进程锁，因此跨线程/跨进程 SQLite 客户端也使用同一持久边界。

## 验收证据

- 索引幂等专项 `10 passed, 1 warning`。
- 并发反例启动两个独立 `IndexRunJournal` 连接，同时提交同一键；无异常，两个结果的 `run_id` 集合大小为 `1`。
- 完整 `scripts/check.ps1`：后端 `1373 passed, 1 warning`；前端生产构建通过，8 组 UI 测试合计 `61 passed`；`compileall` 和 `cargo check --locked` 通过。

## 边界

该改动只覆盖本地索引收据的原子创建和输入绑定，不宣称所有业务表/外部副作用都已达到跨进程 exactly-once，也不替代 SMTP 的 uncertain 状态、记忆 outbox 的 at-least-once 重放或真实 Tauri/WPS UI/E2E。
