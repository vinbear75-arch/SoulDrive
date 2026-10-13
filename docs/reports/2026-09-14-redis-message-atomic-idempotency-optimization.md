# Redis 消息热投影原子幂等优化

日期：2026-09-14

## 发现

`RedisSessionStore.append()` 原先先执行 `SADD(message_id)`，再通过 pipeline 写入消息列表。若列表写入在两步之间失败，Redis 已经留下“消息处理过”的标记，但消息正文并未进入热列表；后续 outbox 重放会因 `SADD` 返回 0 而跳过写入，形成可复现的热投影缺失。

此外，Redis 只按 `message_id` 去重，不检查同一 ID 是否携带了不同会话、序号、角色、正文或元数据。这与本地加密消息事实源和 PostgreSQL 投影的冲突保护不一致。

## 公开实践对照

- [Restate safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)：重试身份必须稳定，幂等键不能把不同输入静默合并。
- [pg-outbox](https://github.com/czeresniowski-dev/pg-outbox)：outbox 消费按至少一次交付设计，派生写入需要可去重、可重放并能暴露冲突。
- [openonce](https://github.com/memovai/openonce)：把意图指纹和副作用账本作为幂等边界，而不是只记录一次调用是否出现。

本轮只把这些原则落到当前 Redis 热投影，不引入新的队列或分布式运行时。

## 实现

- 使用单个 Redis Lua 脚本原子完成：检查已知消息摘要、追加密文列表、裁剪窗口、登记 `message_id` 与身份摘要、刷新 TTL。
- 新增按会话隔离的 `message_digests` hash；身份摘要覆盖 `session_id`、序号、角色、正文和元数据。
- 同 ID 同摘要返回幂等成功；同 ID 不同摘要返回 `message projection conflict`；旧版本只有 `message_ids` 标记时保持兼容，不改写既有消息。
- 删除会话时一并删除消息列表、已见集合、身份摘要、摘要和会话元数据。
- 本地加密消息文件仍是事实源，Redis 仍只是可重建的热投影；这不把系统宣称为跨存储 exactly-once。

## 验证

- 失败优先：旧实现测试先证明“列表写入失败后仍残留 `message_id`”；修复后该反例通过。
- 新增 Redis 原子写入与冲突回归：`1 passed`。
- 会话存储、运行时审计、TaskJournal 定向回归：`97 passed`。
- 完整工程门禁：后端 `1503 passed, 1 warning`；前端生产构建及 8 组脚本 `62 passed`，`compileall` 与锁定 Cargo 通过。
- 本机未安装 `redis-server`/`redis-cli`，因此本轮没有把 fake client 当成真实 Redis 联调；真实 Redis 版本兼容、断线后服务端已执行但客户端未收到响应的重试语义仍需在有 Redis 实例时验证。

## 边界

这保证的是 Redis 热投影的单脚本原子性和输入冲突可见性，不保证外部 SMTP、真实日历、PostgreSQL、Redis 与本地文件之间的全局 exactly-once，也不替代事实内容准确率和认证授权验证。
