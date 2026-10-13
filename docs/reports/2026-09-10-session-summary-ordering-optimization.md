# 会话摘要派生投影乱序保护优化记录

日期：2026-09-10

## 结论

本轮修复了会话压缩摘要的两处耐久同步缺口：加密文件事实源可以按版本保存多个摘要，但 Redis 热投影原来只要收到事件就直接覆盖；会话删除后，迟到的摘要事件也可能重新制造已删除的热状态。现在 Redis 摘要投影按 `version` 单调更新，迟到的旧版本不会覆盖新版本；Redis/PostgreSQL 摘要事件在派生前还必须确认会话仍存在。

## 外部实践对照

- [AWS transactional outbox guidance](https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)指出 outbox 可能产生重复通知，消费者必须幂等，同时事件通知顺序对事件流和点时恢复很重要。
- [microservices.io transactional outbox](https://microservices.io/patterns/data/transactional-outbox)明确要求消息消费者按消息 ID 做幂等，并提醒 relay 在发布后崩溃可能再次发布同一事件。SoulDrive 继续使用本地加密 outbox，不引入消息中间件，只把版本单调性落实到 Redis 摘要消费者。
- [Restate request lifecycle](https://docs.restate.dev/guides/request-lifecycle)将 durable journal 的已提交步骤视为重试时的恢复边界；本地摘要投影同样不能把重试/迟到事件当作比事实源更新的状态。

## 实现

- `RedisSessionStore.set_summary` 在本进程内用锁读取当前摘要版本，只有 `incoming.version >= current.version` 才写入；较旧版本直接返回 `False`。
- 该门同时保护正常写入和 `ProductionSessionStore._dispatch_event` 的 outbox 重放路径；文件摘要和 PostgreSQL 控制面仍保留完整版本历史，Redis 只作为可重建的热投影。
- `redis.session_summary` 与 `postgres.session_summary` 重放前复用归档会话存在性门；会话删除后到达的旧摘要事件被丢弃，不会重新创建热摘要或 PostgreSQL 派生状态。
- 未改变会话事实源、摘要版本分配或跨存储 exactly-once 语义；Redis 不可用时仍由已有文件/outbox 恢复路径承担一致性。

## 失败优先验证

- 新增 `test_redis_summary_projection_does_not_regress_on_late_outbox_event`：先投递版本 2，再投递版本 1，确认热投影仍为版本 2。
- 扩展 `test_deleted_session_outbox_create_events_cannot_resurrect_hot_state`：删除会话后分别重放 Redis/PostgreSQL 摘要事件，确认两个派生投影都不会被重建。
- `tests/test_conversation_store.py`：`37 passed`。
- 上一轮全量 `scripts/check.ps1` 已完成：后端 `1121 passed, 1 warning`；前端生产构建成功，7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。随后 outbox 重放轮次为 `1122 passed`，当前更新后的最新全量见 `docs/reports/2026-09-10-historical-memory-readonly-optimization.md`，为后端 `1123 passed, 1 warning`。

## 边界

- 这是单机进程内 Redis 热投影的版本门，不是跨 Redis、文件和 PostgreSQL 的分布式事务；事件重复投递仍依赖下游操作的幂等性。
- 删除存在性门依赖本地归档事实源；它阻断删除后的迟到事件，但不把删除与所有派生存储变成跨存储原子事务。
- 摘要版本相同但内容不同的异常不会被本轮自动合并；事实源和摘要版本分配仍需由会话存储保证。
- 不建设常驻消息队列、死信服务或多机协调器，保持当前 Windows 单用户 PC 边界。
