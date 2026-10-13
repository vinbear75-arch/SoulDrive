# Redis 会话元数据原子身份幂等优化

日期：2026-09-14

## 发现

`RedisSessionStore.set_session_metadata()` 原先先读取 metadata、在客户端判断项目/标题/workflow 冲突，再执行 `SETEX`。两个进程同时首次初始化同一个 `session_id` 时，读取和写入之间存在 TOCTOU 窗口；不同会话身份可能互相覆盖，且没有可由服务端原子裁决的冲突结果。Redis 消息追加已经使用 Lua 原子脚本，但 metadata 投影仍未对齐。

## 公开实践对照

- [Redis Lua scripting](https://redis.io/docs/latest/develop/programmability/eval-intro/) 明确说明脚本在服务端原子执行，适合把跨 key 的条件读取和写入合并为一个不可插入的操作。
- [Redis Functions](https://redis.io/docs/latest/develop/programmability/functions-intro/) 保持同样的服务端原子语义；本轮沿用项目已有的参数化 `EVAL`，避免引入 Redis 版本或部署方式变化。
- [Restate safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md) 将重试身份绑定到稳定幂等键，并在并发更新时返回可行动的冲突，而不是静默覆盖。

本轮只增强当前 Redis 派生投影，不引入新的队列、锁服务或重量级 durable runtime。

## 实现

- 新增按 session 隔离的 `metadata_identity` digest key，摘要只覆盖不可变身份字段：`project_id`、规范化标题和 `workflow_id`。
- 使用一个 Redis Lua 脚本原子完成：检查已登记的身份摘要、兼容旧版只有 metadata 没有 digest 的记录、条件写入密文 metadata，并同步刷新两把 key 的 TTL。
- 同一身份重试返回幂等成功；已登记的不同身份返回 `session metadata conflict`；旧记录在客户端读取期间被其他 writer 改写时返回 `session metadata concurrent conflict`，fail-closed，不覆盖竞争写入。
- 删除 session 时一并删除 metadata digest；本地加密会话事实源仍是权威，Redis 仍只是可重建热投影。

## 验证

- 失败优先：在修复前新增回归确认实现没有进入原子 metadata 路径，测试先红；修复后通过。
- Redis 会话身份同值重试、服务端身份摘要冲突、旧 metadata 竞争写入不覆盖，以及 Redis 消息原子追加：`2 passed`。
- 会话存储、运行时审计、TaskJournal 定向回归：`102 passed`。
- 完整工程门禁：后端 `1508 passed, 1 warning`；前端生产构建及 8 组脚本 `62 passed`，`compileall` 与锁定 Cargo 通过。本机未安装 `redis-server`/`redis-cli`，因此 fake client 回归不等于真实 Redis 联调。

## 边界

这保证的是单个 Redis 实例内该 metadata 投影脚本的原子条件写入与冲突可见性，不保证 Redis、PostgreSQL、本地加密文件之间的跨存储 exactly-once，也不替代真实 Redis 版本/集群兼容测试、认证授权验证和真实 Tauri/WPS UI/E2E。
