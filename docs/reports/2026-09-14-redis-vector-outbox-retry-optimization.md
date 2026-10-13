# Redis 向量投影失败的 durable outbox 重试优化

日期：2026-09-14

## 结论

本轮修复了一个会丢失 durable execution 语义的真实缺口：Redis 向量投影 `put_vector()` 在索引不可用或写入失败时返回 `False`，而生产存储层此前只把“抛异常”视为失败，可能将未写入的投影当作成功处理，且不进入本地 outbox。

现在向量投影写入统一经过 `_put_redis_vector_or_raise()`：返回 `False` 会转成可观测异常，`_try_backend()` 会保留带版本与身份摘要的重试事件；恢复后重放会再次执行写入，并在成功后完成 outbox 事件。Vector Set 删除的命令异常也不再被吞掉；只有成员不存在才保持幂等成功。

## 外部实践对齐

- Temporal 将 Activity 的丢失、超时、重试和取消视为执行语义的一部分；任务没有可靠完成信号时，不能静默当作成功。参见 [Temporal Activity Execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx)。
- Restate 强调入口重试与 durable invocation 必须避免重复或丢失；这要求业务操作明确区分“已完成”“可重试失败”和“幂等跳过”。参见 [Restate v1.7 release notes](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)。
- 本项目没有引入重量级 durable runtime，而是把同一原则落到已有的本地加密 outbox、Redis/PostgreSQL 派生投影和事实源版本闸门上。

## 实现边界

1. `persist_memory()` 与 outbox 重放路径都检查向量投影的布尔结果。
2. Redis 向量事件保存 `memory_identity_digest`；它由写入事实源时的归档载荷计算，避免向量事件重放时因缺少正文而被误判为同版本冲突。
3. 控制面转发载荷保留该摘要，跨 Redis/PostgreSQL outbox 重放仍能执行版本与身份校验。
4. `rehydrate_memory_vectors()` 仍保留自己的“统计失败、不抛出整批”语义；它是后台修复扫描，不直接复用业务 outbox 的成功判定。

## 回归证据

- 失败优先回归：修复前 `redis_vector_false_result_is_kept_in_outbox_for_replay` 失败，`False` 返回导致 pending outbox 数量为 0；删除异常回归也证明 `VREM` 异常被吞掉时不会入队。
- 修复后：`tests/test_conversation_store.py` 定向回归 `77 passed`。
- 新回归覆盖：写入返回 `False` → 本地 outbox 保留事件 → 恢复后写入返回成功 → 事件完成且不再 pending；删除命令异常 → 删除事件进入 outbox；同时验证控制面载荷保留身份摘要。
- 完整工程门禁：后端 `1512 passed, 1 warning`；前端生产构建成功；8 组 UI 脚本合计 `62 passed`；`compileall` 与锁定 Cargo 通过。该最终计数包含 Vector Set 删除命令异常进入 durable outbox 的新增回归。
- 本轮尚未连接真实 Redis 8/RediSearch 或 PostgreSQL 实例；真实后端网络、脚本和 Vector Set 行为仍需在可控环境做集成验证。
