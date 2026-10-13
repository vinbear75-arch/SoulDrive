# 记忆同步 outbox 过期 lease fencing 优化

日期：2026-09-12

## 结论

本轮修复了 memory sync outbox 的租约过期窗口。此前事件被 worker claim 后，`complete_memory_sync` 和 `fail_memory_sync` 只比较 `claim_token`；在 `claim_until` 已过期但新 worker 尚未重新 claim 的窗口内，旧 worker 仍可能提交完成或失败回执。

现在带 `claim_token` 的完成/失败回执同时要求 `claim_until > now`。过期 token 会返回 `False`，事件保持 pending 并可被下一 worker 重新 claim；没有 claim 的初始同步仍保持原有路径，避免把“主库写入后立即确认未 claim 的 outbox”误当成 worker lease。

## 外部实践对照

- [Temporal activity state](https://github.com/temporalio/temporal/blob/main/chasm/lib/activity/proto/v1/activity_state.proto) 使用 attempt-scoped stamp 和 task token 区分当前 attempt 与旧 attempt，旧任务不能凭旧身份更新活动状态。
- [Apache Kafka consumer group](https://github.com/apache/kafka/blob/trunk/group-coordinator/src/main/java/org/apache/kafka/coordinator/group/modern/consumer/ConsumerGroup.java) 在 offset/assignment 操作中校验 member epoch，旧成员的提交会被识别为 stale；[KafkaConsumer 文档](https://github.com/apache/kafka/blob/trunk/clients/src/main/java/org/apache/kafka/clients/consumer/KafkaConsumer.java) 也把 rebalance 后的提交失败作为只允许当前活跃成员提交的安全机制。
- [Celery task 文档](https://github.com/celery/celery/blob/main/docs/userguide/tasks.rst) 明确指出 worker 失败后消息会重新投递，任务应尽量幂等；这说明可见性/租约到期只能触发重试，不能让旧 worker 的晚到回执继续生效。

本地只迁移“租约有期限，回执必须在有效期限内提交”的原则；没有引入消息队列、分布式锁或外部调度器。

## 失败优先验证

- 新增 `LearningStoreTests.test_memory_sync_outbox_expired_claim_cannot_ack_before_reclaim`。
- 修复前：worker A claim 60 秒后，将时间推进到第 61 秒，旧 token 仍可令 `complete_memory_sync()` 返回 `True`。
- 修复后：过期 token 的完成和失败回执均返回 `False`；worker B 能重新 claim，并用新 token 成功完成。

## 实现与验证

- `core/learning_store.py::complete_memory_sync` 在带 token 的 UPDATE 中增加 `claim_until > now`。
- `core/learning_store.py::fail_memory_sync` 在查询和 UPDATE 两个阶段都增加同一有效期条件，避免先查到后更新时绕过 fencing。
- 过期 claim、当前 claim 和独占释放专项：`3 passed`。
- 学习存储 + MCP 学习组合：`135 passed, 1 warning`。
- 本轮完整 `scripts/check.ps1`：后端 `1143 passed, 1 warning`；前端生产构建与 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。它只代表本机 Python/前端/Cargo 自动门，不代表真实 Tauri/WPS 或安装包交付。

## 边界

- 这是本机 SQLite outbox 的有效期 fencing，不是跨机器共识，也不证明外部 Redis/PostgreSQL/模型提供方的 exactly-once。
- 如果旧 worker 在 lease 到期前已经成功触发外部副作用，随后才发现本地回执过期，重复风险仍需下游幂等键、结果查询或人工核验。
- 这里保持“过期后可重试”的 at-least-once 语义，没有把无法证明的重复副作用包装成成功。
