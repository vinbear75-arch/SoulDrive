# 记忆投影项目范围归一化与 replay 恢复

日期：2026-09-14

## 外部实践与缺口

对照 [Mem0 memory implementation](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py) 对 tenant-scoping 字段的保护，以及 [Graphiti MCP provenance](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 对 group/provenance 的保留，记忆事实源、向量投影和控制面投影必须使用同一范围身份。

SoulDrive 的 `ProductionSessionStore.persist_memory()` 已经从 memory payload 归一化项目范围，用于本地事实源和 Redis vector；但写 PostgreSQL projection 与 `postgres.memory` outbox 时仍传入调用参数 `project_id`。恢复事件或内部调用只在 memory payload 中携带项目时，PostgreSQL 会落成 `NULL`，造成控制面与本地/Redis 范围不一致。

同一问题还存在于 `redis.memory_vector` outbox replay：事件 payload 可能是旧格式或缺少项目字段，而本地归档仍保存权威的 `scope/project_id`。如果 replay 只信 payload，就会把项目记忆的向量投影恢复为全局向量。

进一步的失败优先回归表明，若项目只通过 `persist_memory(..., project_id=...)` 参数传入，原实现连本地归档也没有保存规范化范围；因此恢复无法从事实源补回。撤销路径存在同样问题，并且本地 PostgreSQL outbox 携带的半规范 memory payload 会在版本冲突门被错误拒绝。

## 实现与回归

- 持久化路径统一使用 `normalized_project_id` 调用 PostgreSQL，并写入 outbox payload；global memory 继续强制为 `NULL`。
- revoke 路径采用同一归一化规则，避免撤销事件和恢复事件出现不同范围。
- PostgreSQL replay 在 payload 缺少项目时回读本地 memory archive，并以事实源中的范围恢复投影；global memory 继续强制为 `NULL`。
- Redis vector replay 同样回读本地 memory archive，补齐 `project_id`、`scope`、版本、有效期和 workflow 等投影元数据；payload 明确提供的向量仍作为重放内容。
- `persist_memory`/`revoke_memory` 先把 `scope/project_id/record_version` 写入加密本地事实源；本地 PostgreSQL outbox 的 memory payload 使用同一规范化快照，避免 replay 版本冲突。
- 失败优先用例覆盖“payload 有项目、调用参数为空”的在线 PostgreSQL projection、PostgreSQL/Redis replay 缺少项目字段、项目参数只在调用参数中出现以及撤销 replay；相关 durable memory 定向 `7 passed`，整份会话存储回归 `87 passed`，学习/会话/记忆/MCP 组合基线 `316 passed, 1 warning`。

## 边界

这只证明本地归一化和 mock/replay 路径的项目身份一致，不证明真实 PostgreSQL/Redis 连接、并发事务、跨存储原子提交或 exactly-once；真实存储实例联调仍是待验收项。

本轮完整工程门禁随后通过：后端 `1545 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 检查通过。该门禁仍不等价于真实 Redis/PostgreSQL 实例、跨存储 exactly-once 或真实 Tauri/WPS UI/E2E。
