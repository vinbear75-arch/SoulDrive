# Outbox 事件身份与失败重放优化

日期：2026-09-14

## 发现

本地加密 outbox 的状态记录是追加快照，但 `mark_outbox()` 失败后只写入 `event_id/status/error`，会把原事件类型和 payload 从最新快照中遮蔽；下一次重放无法知道要执行什么。

同时，PostgreSQL 控制 outbox 使用 `ON CONFLICT(event_id) DO NOTHING`，没有校验已有事件的类型、聚合对象和 payload。相同事件 ID 若被错误复用，新的输入会被静默吞掉，诊断无法区分“安全重试”和“事件身份损坏”。

## 公开实践对照

- [durable-sync](https://github.com/isaacrowntree/durable-sync)：不可变主键对应不可变事件身份，重放不应把不同输入合并到一个 key。
- [Tandem transactional outbox](https://github.com/alirux/tandem)：事务 outbox 需要稳定的聚合/事件语义，状态推进不应丢失原始事件载荷。
- [pg-outbox](https://github.com/czeresniowski-dev/pg-outbox)：按至少一次交付消费，派生副作用必须支持安全重放和冲突可见性。

## 实现

- 本地 outbox 增加由 `event_type`、`aggregate_id` 和规范化 payload 组成的 `identity_digest`。
- `mark_outbox()` 先读取同一事件的最新完整快照，再只更新状态和错误，确保失败重试保留原始 dispatch payload。
- 读取和更新时兼容旧版本已经写出的 status-only 半快照：向前合并同一 `event_id` 的最近完整语义，避免升级后还需要手工清理 outbox。
- 同一 `event_id` 的不同事件身份在本地追加时返回 `outbox event conflict`，不覆盖旧事件。
- 对消息事件忽略可由事实源重建且会随重试变化的 `session_metadata.updated_at`/manifest 提示，只以消息记录和目标路径作为不可变 dispatch identity。
- PostgreSQL outbox 增加 `payload_hash` 列及旧表迁移语句；插入前后均校验已有事件身份，旧行没有 hash 时从已有 payload 兼容计算。
- 本地文件 outbox 仍是事实源，PostgreSQL 仍是控制面派生投影；没有宣称跨存储 exactly-once。

## 验证

- 失败优先反例：旧实现先证明 `mark_outbox()` 后丢失 `event_type/payload`；修复后通过。
- 本地 outbox 同 ID 冲突与重试 payload 保留：通过。
- 旧格式 status-only 半快照恢复原事件：通过。
- PostgreSQL outbox 同 ID 同输入幂等、不同输入冲突：通过。
- 会话存储、运行时审计、TaskJournal 定向回归：`99 passed`。
- 完整工程门禁：后端 `1508 passed, 1 warning`；前端生产构建及 8 组脚本 `62 passed`，`compileall` 与锁定 Cargo 通过。本机没有真实 PostgreSQL 实例，控制面 SQL 仍需实例级联调。

## 边界

这保证的是 outbox 事件身份和本地失败重放不会静默丢失输入，不保证外部副作用的 exactly-once、跨存储原子提交、消息队列的全局顺序或模型答案准确率。
