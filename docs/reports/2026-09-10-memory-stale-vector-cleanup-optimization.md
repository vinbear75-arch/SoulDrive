# 记忆更新时旧语义向量清理优化

日期：2026-09-10

## 结论

本轮修复了长期记忆更新与可选 Redis 语义投影之间的版本一致性缺口。

旧行为是：活动记忆更新成功，但 embedding 暂时不可用或被明确延迟时，只更新本地 SQLite/加密归档，不触碰 Redis 中旧向量。之后语义检索仍可能返回该 `memory_id`，但向量代表的是旧标题/旧内容；关键词事实源已经是新内容，两个信号发生语义漂移。

现在的行为是：

- 新版本有 fresh embedding：继续 upsert 新向量；
- 新版本没有 embedding：立即删除该记忆的 Redis 投影；
- Redis 删除失败：写入现有本地加密 outbox，保留 `record_version`，恢复后按既有 fencing 重放；
- 删除期间普通问答仍可使用 SQLite 关键词记忆，不把暂时没有向量误报成事实缺失。

## 外部实践对照

- [Qdrant Data Synchronization](https://qdrant.tech/documentation/data-synchronization/) 将向量库视为派生系统，推荐事件驱动同步、批量按时间/版本重建，或双写加 reconciliation，核心是避免源库与向量库长期漂移。
- [Mem0 memory implementation](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) 的记忆更新/实体清理路径会重新嵌入更新对象，或删除不再有有效关联的向量实体；不会让旧向量继续代表新事实。
- [Qdrant consistency guarantees](https://qdrant.tech/documentation/scaling/consistency-guarantees/) 强调更新/删除顺序和读写一致性需要显式处理；SoulDrive 当前单机边界用本地 `record_version` 与 outbox fencing 实现等价的最小约束，没有引入 Qdrant。

## 代码变化

`core/state_backends.py` 的 `ProductionSessionStore.persist_memory` 在 `vector` 为空且 Redis 启用时，发送 `redis.memory_vector_delete`。事件带 `memory_id` 和新 `record_version`；Redis 正常时同步删除，异常时由现有 `_try_backend` 形成待重放事件。

该分支不改变事实源：SQLite/加密归档仍保存记忆内容，Redis 只是可选派生语义投影。没有 embedding 时选择“无旧语义投影 + 关键词回退”，而不是继续使用可能错误的旧向量。

## 失败优先与验证

- 新增回归 `test_memory_update_without_embedding_removes_stale_vector_projection`：旧实现未调用 `remove_vector`，先失败；修复后确认新版本不会调用 `put_vector`，而会精确删除对应 `memory_id`。
- 记忆/会话/MCP 组合回归：`152 passed, 1 warning`。
- `scripts/check.ps1`：后端 `1112 passed, 1 warning`；Python compileall 通过；前端生产构建通过；前端 7 组测试共 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮没有实现 Redis 重启后的全量向量自动重建，也没有声称语义记忆答案质量提升。当前只保证“没有 fresh embedding 时不继续使用旧投影”。已生成 embedding 是否应以加密派生形式保存到本地事实归档、以及启动时如何做有界重建，仍需下一轮在隐私、版本、磁盘和启动延迟约束下单独验证。
