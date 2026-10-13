# 语义记忆向量投影 TTL 隔离优化

日期：2026-09-10

## 结论

本轮确认并修复了一个会让长期记忆“静默退化”的真实缺口：Redis 会话缓存和语义记忆向量投影原先共用 `SOULDRIVE_REDIS_TTL_SECONDS`。默认七天 TTL 到期后，SQLite 加密归档中的记忆仍然存在，关键词检索仍可工作，但 Redis 语义投影会无告警消失；由于当前没有历史向量自动回填，这会让语义记忆召回永久退化到关键词路径。

现在两类状态分离：

- 会话消息、摘要和元数据继续使用短期 Redis TTL；
- 语义记忆向量投影默认不设置过期，并在写入时清除旧投影可能遗留的 TTL；
- 如部署明确希望向量投影只是临时缓存，可单独设置 `SOULDRIVE_REDIS_MEMORY_VECTOR_TTL_SECONDS`，不再隐式继承会话 TTL；
- Redis 健康信息公开 `memory_vector_ttl_seconds` 和 `memory_vector_expiration_policy`，避免把“投影可用”误报成“长期事实源”。SQLite/加密归档仍是事实源，Redis 仍是可重建派生索引。

## 外部实践对照

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 明确区分 thread 级 checkpointer 与跨会话的长期 Store；长期 Store 不应因为短期线程状态的生命周期而静默消失。
- [LangGraph memory](https://docs.langchain.com/oss/python/langgraph/add-memory) 将短期记忆和跨会话长期记忆作为不同能力和存储契约。
- [Graphiti](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 将事实的 `valid_at`/`invalid_at` 时间有效性显式建模，保留历史而不是用无提示的缓存过期替代事实生命周期。

这些项目不是 SoulDrive 的依赖引入目标；它们只支持一个最小映射：短期缓存 TTL 与长期记忆投影策略必须显式分开，时间有效性不能与缓存生存期混为一谈。

## 代码变化

- `core/state_backends.py`
  - `RedisSessionStore` 新增独立 `memory_vector_ttl_seconds` 参数，默认 `None`（不设置过期）。
  - Vector Set 写入使用 `PERSIST` 清除历史集合 TTL；RediSearch hash 写入使用 `PERSIST` 清除历史 key TTL。
  - 仅在显式配置正数 TTL 时调用 `EXPIRE`，且该 TTL 与会话 `ttl_seconds` 独立。
  - `ProductionSessionStore` 从 `SOULDRIVE_REDIS_MEMORY_VECTOR_TTL_SECONDS` 读取配置，`0`/缺省表示不设过期。
  - Redis health 暴露投影过期策略和秒数。
- `tests/test_conversation_store.py`
  - 先失败后修复的回归确认 `ttl_seconds=600` 不会为 Vector Set 记忆投影写入 `600` 秒过期；同时确认会清除遗留 TTL，并在 health 中报告 `no_expiry`。

## 验证

- 失败优先：旧实现实际记录 `('souldrive:memory_vectors', 600)`，回归失败；修复后该回归通过。
- 语义向量/会话/MCP 组合：`94 passed, 1 warning`。
- `scripts/check.ps1`：后端 `1111 passed, 1 warning`；Python compileall 通过；前端生产构建通过；前端 7 组测试共 `48 passed`；Tauri `cargo check --locked` 通过。
- 前端构建仍报告既有的单 chunk 超过 500 kB 警告；Starlette/httpx 弃用提示仍是环境警告，不是本轮失败。

## 边界

本轮没有声称 Redis 是 durable source，也没有实现历史向量自动回填、向量质量评测或答案准确率提升。Redis 重启、AOF 配置、索引损坏和旧投影重建仍需通过既有 outbox/人工诊断处理；没有 Redis 或 embedding 时，系统仍必须回退到 SQLite 关键词链路。
