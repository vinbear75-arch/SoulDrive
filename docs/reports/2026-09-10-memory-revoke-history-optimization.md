# SoulDrive 记忆停用后的历史回溯优化

日期：2026-09-10

## 结论

本轮修复了记忆生命周期中的一个真实时间线缺口：`archive_memory` 原先会把当前记忆和全部 `memory_versions` 一起标成 `archived`。这样“现在停用”不仅阻止了未来注入，也会让停用前的 `as_of` 查询找不到曾经有效的事实，和系统已经对外承诺的历史回溯语义不一致。

现在停用分成两个效果：

- 当前工作区查询不再返回这条记忆，派生索引继续通过 `memory_revoke` outbox 移除；
- 停用发生前的有效版本写入 `memory_versions`，以 `invalid_at=停用时刻` 结束有效区间，因此显式历史查询仍能恢复当时的内容；已存在的历史版本继续保留；尚未到 `valid_from` 的计划记忆没有历史有效区间，不创建快照。

硬删除仍保持原语义：删除当前记录和活动索引，不把删除误报成可回溯的撤销。

## 外部实践对照

[Graphiti 的官方 MCP 实现](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)把记忆事实建模为带 `valid_at`/`invalid_at` 的时间事实；新信息可以使旧事实失效，但历史仍可被保留和解释。SoulDrive 没有引入 Graphiti 的图数据库或实体抽取，只把其中最小且与本地记忆表直接对应的“失效不等于历史删除”原则落到 `memory_versions`。

[LangGraph 的记忆文档](https://langchain-ai.github.io/langgraph/how-tos/memory/manage-conversation-history/)也区分当前短期状态、长期存储和历史管理；本轮继续保持 SoulDrive 的短期会话、当前长期记忆和显式历史时点三层边界，不把历史版本自动注入普通问答。

## 实现与验证

- `core/learning_store.py`：`archive_memory` 在归档当前记忆前解密当前版本，按 `valid_from` 或创建时间与撤销时间构造历史有效区间，幂等写入 `memory_versions`；不再批量归档已有历史版本。
- `tests/test_learning_store.py`：新增停用后历史回溯回归；同时复跑编辑版本历史和 revoke/delete 幂等回归。
- 定向停用/历史/幂等：`3 passed`。
- 完整 `LearningStore`：`52 passed`。
- 学习存储、学习 API、聊天上下文、记忆 runtime/gate 组合：`126 passed, 1 warning`。
- 仓库级 `scripts/check.ps1`：后端 `1102 passed, 1 warning`；Python compileall 通过；前端构建和 `48 passed` 通过；Tauri `cargo check --locked` 通过。

## 明确边界

- 这不是 GraphRAG，也没有增加 Neo4j、Graphiti、后台 dreaming 或新的向量服务。
- 这只修复本地事实层的时间线与撤销关系，不证明语义记忆或最终答案准确率提升。
- 普通 `/chat` 仍只读取当前有效记忆；只有显式解析出的历史时点才会查询历史版本。
- Redis/PostgreSQL/归档文件仍是派生同步目标；本轮没有把跨存储操作宣称为 exactly-once。
