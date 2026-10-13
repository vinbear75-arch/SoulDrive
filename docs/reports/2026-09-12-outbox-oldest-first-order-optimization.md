# Outbox 最老事件优先与稳定顺序优化

日期：2026-09-12

## 结论

本地加密 `ConversationStore.pending_outbox()` 与可选 PostgreSQL `PostgresStateStore.pending_outbox()` 现在统一按 `created_at ASC, event_id ASC` 取 pending 事件。这样批量上限下会优先处理最老事件，同一时间戳也有稳定的事件身份顺序；状态过滤、事件去重和现有串行 replay 锁没有改变。

## 外部实践对照

- [AWS transactional outbox guidance](https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)：指出 outbox 消费应保持数据库更新的通知顺序，并建议用时间戳与序列号保持顺序；重复通知仍需消费者幂等。
- [Temporal Event History](https://github.com/temporalio/temporal/blob/main/docs/architecture/history-service.md)：把事件历史视为可恢复状态的线性序列，重放依赖同一事件顺序。
- [Temporal durable execution/replay](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-execution/workflow-execution.mdx)：崩溃后从持久事件历史继续执行，顺序一致性是可重放性的基础。

这些机制只作为本地 outbox 的最小设计参考；SoulDrive 没有因此引入消息代理、分布式锁或 Temporal 运行时。

## 失败优先验证

新增两个回归：

- 本地归档以乱序时间写入 `evt-new(20)`、`evt-old(10)`、`evt-tie-b(30)`、`evt-tie-a(30)`；修复前 `pending_outbox(limit=4)` 按文件追加顺序返回，修复后稳定返回 `evt-old, evt-new, evt-tie-a, evt-tie-b`。
- PostgreSQL 适配器的伪游标先返回同时间 `evt-tie-b/evt-tie-a`；修复前查询没有稳定末级排序，修复后 SQL 使用 `ORDER BY created_at ASC, event_id ASC`。

## 回归证据

- `tests/test_conversation_store.py`：`40 passed`（在项目指定 `souldrive` Conda 环境中运行）。
- `scripts/check.ps1`：后端 `1155 passed, 1 warning`，Python compileall 通过。
- 前端生产构建通过，7 组前端测试共 `48 passed`。
- Tauri `cargo check --locked` 通过。

## 边界

这只修复 pending 批次的公平性和确定性，不提供跨存储原子提交、外部副作用 exactly-once、消息代理顺序保证或答案质量提升。`replay_outbox()` 仍是本机、串行、可重放的 at-least-once 机制，外部邮件/日历等副作用仍需业务幂等键、结果查询或人工核验。
