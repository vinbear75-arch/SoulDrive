# 项目删除与学习数据生命周期清理优化

日期：2026-09-14  
范围：项目删除 durable receipt、项目级 Skill/记忆、learning chunk、Skill run、记忆归档、Redis/PostgreSQL 派生投影和待投递 outbox。

## 本轮结论

本轮确认了一个真实的生命周期缺口：项目删除原先只清理项目目录、文档/图索引、会话和 ProjectCatalog 级联数据；项目级 LearningStore 行、Skill 运行记录、学习检索块、记忆历史/幂等收据、项目记忆同步 outbox，以及会话存储中的加密记忆归档仍可留存。项目已从目录层“删除”变成了学习数据层仍可恢复的半删除状态。

现在项目删除的顺序是：

1. 先读取项目 namespace 的不透明 ID 快照；
2. 先删除本机记忆归档，并对 Redis/PostgreSQL 记忆投影执行幂等删除；外部不可用时沿用本地加密 outbox，待恢复后重放删除；
3. 原子清理项目级 memory/Skill、Skill run、learning chunk、历史版本、memory mutation receipt、learning sync outbox、项目检索反馈和不再被 global 资产引用的审核候选；
4. 再删除会话、文档/图索引、原始项目目录和 ProjectCatalog 行；
5. 最后写入既有 `project_deletions` completed receipt，重复请求直接回放已完成结果。

Global Skill、global memory 以及被 global memory 引用的审核候选不会因删除某个项目而被误删。没有学习数据库时保持兼容的 `not_present` 结果；数据库存在但工作区密钥不可用时 fail-closed，不继续删除项目目录。

## 外部实践对照

- [Mem0 SDK lifecycle](https://github.com/mem0ai/mem0/blob/main/skills/mem0/references/sdk-guide.md) 把 `delete(memory_id)`、按实体范围的 `delete_all` 与 history 分开，说明批量删除必须绑定明确的 user/agent/app/run 范围，不能用无范围的全表清理。
- [Upstash Vector namespace deletion](https://github.com/upstash/vector-py) 将向量 namespace 视为可整体 reset/delete 的派生范围；SoulDrive 保留本机 SQLite 主数据，但把 `project_id` 作为同等强度的派生投影删除边界。
- [Temporal workflow deletion stages](https://github.com/temporalio/temporal/blob/main/service/history/shard/context_impl.go) 将删除拆成有顺序、可重试的阶段，并明确每个阶段应可安全重试；SoulDrive 沿用该原则，但仍是单机多存储补偿，不宣称跨存储原子事务。

本轮没有引入 Mem0、Upstash、Temporal 或新的远程服务，只把 namespace、派生索引清理和分阶段重试的可验证边界映射到现有本机结构。

## 失败优先证据

- 新增项目删除反例：项目目录删除成功后，旧实现仍能从 LearningStore 读出项目 Skill，记忆归档文件仍存在；修复前用例失败，修复后通过。
- 新增 LearningStore 反例：项目 memory/Skill/Skill run/chunk 与 global 资产同时存在；清理后项目范围为空、global 仍可读，第二次清理返回零变更。
- 新增记忆派生反例：归档删除前存在待投递的 `postgres.memory` upsert；清理后归档消失、旧事件被 fence，不会由 outbox 重放复活。
- 项目删除接口回归继续覆盖：索引清理失败保留项目和目录、根目录失败后可重试、完成后审计失败只返回 warning、完成 receipt 可回放。

## 代码变化

- `LearningStore.project_scope_snapshot()` 统一收集项目 memory/Skill 及其子记录 ID；`delete_project_scope()` 在一个 SQLite 事务内删除项目 namespace，保留 global 资产和仍被 global 资产引用的候选。
- `ConversationStore` 增加项目记忆归档扫描、精确 scope 校验、物理删除和本地 outbox fence；不解密失败或跨项目记录，不静默扩大删除范围。
- `ProductionSessionStore` 增加项目记忆投影清理；Redis 向量删除和 PostgreSQL 记忆行删除都使用可重试事件，旧 memory upsert 在本地归档已消失时被识别为 stale，不能复活已删除数据。
- `/projects/{project_id}` 在清理项目目录之前执行学习 namespace 和记忆投影清理；LearningStore 不可解锁时停止删除，避免只删目录留下密文数据。

## 验收结果

本轮定向测试已通过：

- `tests/test_learning_store.py`：`80 passed`；
- `tests/test_conversation_store.py`：`46 passed`；
- `tests/test_mcp_server_projects.py`：`35 passed, 1 warning`；
- `tests/test_mcp_server_learning.py`：`112 passed, 1 warning`；
- Skill runtime/E2E：`11 passed, 1 warning`；
- `compileall`、`git diff --check`：通过。

完整 `scripts/check.ps1` 已通过：后端 `1411 passed, 1 warning`；前端生产构建通过，8 组脚本测试合计 `61 passed`；Python `compileall` 与锁定 Cargo 通过。它仍不替代真实 Tauri/WPS UI、打包启动、Redis/PostgreSQL 生产连接和外部删除核验。

## 明确边界

- 当前删除协议是本机单用户的多阶段补偿，不是 SQLite、文件系统、Redis 和 PostgreSQL 的跨存储原子提交。
- 外部投影删除失败时由现有 outbox 保留至少一次删除事件；不宣称外部 exactly-once，也不自动确认远端最终已删除。
- 无法解密的损坏归档不会依据文件名猜测项目归属；需要由 storage health/人工恢复处理。
- 本轮只清理显式 `scope='project'` 且 `project_id` 精确匹配的数据，不新增自动记忆抽取、自动合并、Agentic RAG 或语义答案质量结论。
