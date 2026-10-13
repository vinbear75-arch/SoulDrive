# Redis 摘要投影跨进程 CAS 优化

日期：2026-09-14

## 结论

本轮修复了 Redis 会话摘要热投影的并发窗口：此前实现只有单进程 `RLock`，先读取当前摘要，再执行 `SETEX`；多个进程或多个 sidecar 实例同时提交摘要时，后写入者可能覆盖已经变化的版本，破坏“同版本不可变、迟到版本不回退”的 durable 投影契约。

现在摘要、身份摘要和版本号由一个 Redis Lua 脚本执行条件更新：

- 同一版本、同一身份摘要可安全重放；
- 同一版本、不同内容返回 `summary version conflict`；
- 迟到旧版本返回不更新；
- 读到当前值后、提交前发生外部写入时返回 `summary concurrent conflict`，不覆盖竞争者的值；
- 旧 Redis 摘要没有辅助身份/版本键时，仍从密文摘要兼容读取，并在下一次成功写入时补齐派生键。

## 外部实践对齐

- [Redis Lua scripting](https://redis.io/docs/latest/develop/programmability/eval-intro/) 明确保证脚本原子执行，可在服务端把条件读取与多键写入合并为一个不可交错的操作。
- [Redis transactions](https://redis.io/docs/latest/develop/using-commands/transactions/) 将 `WATCH`/`EXEC` 定义为乐观锁式 check-and-set；本项目使用 Lua 保持三键更新的一致性，避免客户端读写间隙。
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 将 checkpoint 作为可恢复的持久状态，并要求按 thread/checkpoint 读取，而不是把状态当作无约束缓存；本项目沿用该边界，但不引入 LangGraph 依赖。

## 实现边界

1. `summary` 密文、`summary_identity` 和 `summary_version` 使用同一个脚本更新，并分别设置与摘要相同的 TTL。
2. 脚本在覆盖前校验调用方读取到的密文；密文已变化即 fail-closed，不靠进程内锁假设单实例部署。
3. 现有文件事实源和 PostgreSQL 投影仍是权威/控制面，Redis 仍是可重建热投影；本轮只收紧 Redis 投影的并发一致性，不宣称跨存储事务或 exactly-once。

## 回归证据

- 失败优先：竞争写入测试验证旧的读后写会覆盖竞争值；修复后返回 `summary concurrent conflict`，竞争值保持不变。
- Redis 摘要定向回归：`3 passed`。
- 会话存储、TaskJournal、记忆运行时和 MCP 学习组合：`165 passed, 1 warning`。
- 完整工程门禁：后端 `1513 passed, 1 warning`；前端生产构建成功；8 组 UI 脚本 `62 passed`；`compileall` 与锁定 Cargo 通过。
- 本机没有真实 Redis 实例；Redis 集群/故障转移、真实 TTL 竞态和版本兼容仍需在可控集成环境验证。
