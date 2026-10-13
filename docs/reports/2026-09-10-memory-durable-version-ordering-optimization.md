# SoulDrive 记忆耐久同步的版本单调性优化

日期：2026-09-10

## 外部实践对照

- [Mem0 Memory History](https://mem0.mintlify.app/api-reference/memory/history-memory)把记忆的 `ADD`、`UPDATE`、`DELETE` 变化、旧值、新值和时间作为可查询历史，而不是只保留最后一份文本。
- [Graphiti Persisting](https://graphiti.dev/concepts/persisting/)强调持久化过程中的事务边界和校验；对 SoulDrive 可迁移的重点是：派生状态写入必须服从已验证的当前事实，失败重放不能把旧状态重新写回去。
- [Letta Memory & dreaming](https://github.com/letta-ai/letta-docs-md/blob/main/configuration/memory/index.md)将记忆编辑、审计和版本化文件视为长期记忆的一部分，而不是一次性的提示词拼接。

这些项目没有被作为依赖引入；本轮只抽取“记忆变化可追溯、派生写入按版本单调推进”的机制。

## 本地可复现缺口

SoulDrive 的 `LearningStore` 已经用 `memory_id + record_version` 生成学习同步 outbox 事件，但下游仍存在两个风险：

1. 加密记忆归档文件按 `memory_id` 覆盖；旧版本重试晚于新版本时可能回退文件内容。
2. Redis/PostgreSQL 派生事件此前按 `backend + aggregate_id` 生成事件键，同一记忆的不同版本可能共用一个键；后来的失败事件会被 `ON CONFLICT DO NOTHING` 丢掉，旧事件也可能晚到覆盖新状态。

这不是“测试没覆盖”的抽象担忧：已有学习 outbox 的租约和退避机制允许旧事件在新事件之后重新变得可执行，因此必须在派生层再次检查版本。

## 最小实现

- `ConversationStore.archive_memory` 在写入前读取已有归档；当已有 `record_version` 更高时确认旧重放但不覆盖文件。
- `ProductionSessionStore.persist_memory` 与 `revoke_memory` 在归档后再次检查当前版本；旧版本直接视为已处理，不调用 Redis/PostgreSQL 派生写入或删除。
- Redis 向量投影携带 `record_version`；PostgreSQL 控制面元数据也保存该版本，并在 upsert 前拒绝更旧版本。
- 派生 outbox 事件键改为 `backend_memory_id_v<record_version>_<event_type>_<status>`；不同版本、更新/删除操作和同版本状态变更不再互相吞掉。
- outbox 重放入口在分发前按本地加密归档的最新版本过滤旧的 memory upsert/vector/delete 事件；旧事件被确认完成但不产生副作用。
- 兼容旧数据：缺少版本字段的记录按版本 `1` 处理；没有引入后台 worker、分布式锁或新的数据库依赖。

## 回归与验证

- 先写失败回归：旧版本覆盖新版本、不同版本事件键冲突；两项均按预期失败。
- 修复后新增记忆耐久定向回归：`4 passed`；既有记忆/会话/HTTP组合回归在本轮前一阶段为 `138 passed, 1 warning`。
- 项目级 `scripts/check.ps1`：后端 `1091 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。
- 新增覆盖：归档单调性、派生事件版本键、旧删除/控制面事件无副作用、Redis 版本元数据传递，以及现有向量范围/有效期过滤回归。

## 边界

- 这是本机事实源到派生文件/Redis/PostgreSQL 的版本单调保护，不是跨存储原子提交，也不是外部服务 exactly-once。
- 发生“外部写入成功但本地完成标记失败”时仍可能重复调用；版本保护只防止旧版本回退，不替代提供方幂等键、结果查询或人工核验。
- 当前语义记忆向量投影仍未接入普通 `/chat`：查询 embedding、混合排序和冻结评测集尚未完成，因此没有把本轮持久化修复宣称为答案质量提升。
