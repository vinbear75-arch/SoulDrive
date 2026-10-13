# 项目级 Skill 命名空间优化

日期：2026-09-14  
范围：Skill 发现、LearningStore 入库、项目聊天注入、匹配/计划/运行、PPT 风格读取、管理 API 与 UI 请求。

## 结论

本轮确认并修复了一个真实的跨项目边界缺口：Skill 的 `workflow_ids` 已经能够隔离会话窗口，但 `skills` 主表、显式读取、删除、运行记录和 workspace Skill 同步仍按 workspace 处理。两个项目使用相同相对 `source_path` 时，后同步的 Skill 会覆盖先同步的 Skill；即使 `learning_chunks` 已有项目字段，主表和管理入口仍可能返回越界 Skill。

现在的最小落地协议是：

- workspace 根目录下的现有 `skills/` 仍是 `global` 命名空间，保持兼容并可被项目复用；
- 项目根目录下的 `<project_root>/skills/` 是显式 `project` 命名空间，只在绑定该项目时同步；
- 同一相对 `source_path` 只在同一命名空间内更新，不会跨项目覆盖；
- `list/get/search/delete/archive/skill run` 与聊天 Skill 注入统一使用 `project_id` + `include_global` 范围；无项目上下文时不返回项目 Skill；
- 项目安装、审核、激活、PPT 风格读取、删除和 UI 刷新均沿用相同项目路径；
- 旧数据库迁移为 `skills.scope='global'`，不把历史共享 Skill 静默归入任一项目。

这采用了公开实现中“线程状态与长期存储分层、长期存储使用显式 namespace”的核心边界。LangGraph 的 Store 以 tuple namespace 区分可跨 thread 访问的长期记忆，并强调 namespace 可以表达任意业务边界；SoulDrive 用本地 SQLite 的 `scope/project_id` 映射同一思想，没有引入新的数据库或远程依赖。[LangGraph Stores](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/stores.mdx)

Mem0 对 procedural memory 的分类说明了程序性知识可以长期复用，但并不意味着所有工作流知识都应无范围共享；本轮因此保留全局 Skill，同时给项目 Skill 和 workflow Skill 分别设置边界。[Mem0 memory types](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-types.mdx)

Graphiti 的 MCP 入口把 group、时间和 provenance 作为 episode 作用域，本轮借鉴的是“来源边界必须显式携带”的原则，而不是引入 Graphiti 图数据库。[Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)

## 失败优先证据

新增的跨项目反例先于修复运行：

1. 项目 A 与项目 B 使用相同 `review/SKILL.md`，旧实现第二次 `sync_project_skill()` 返回 `updated`，覆盖项目 A；
2. 修复后两次同步均为 `imported`，生成不同 `skill_id`，且 `list_skills(project_id=...)` 只能看到所属项目；
3. 项目 A 显式读取项目 B 的 `skill_id` 返回 `None`；
4. 项目 A 的 context-only Skill 检索不会返回项目 B 的唯一词；
5. 两个项目各自的 `<project_root>/skills/` 通过 HTTP `GET /learning/skills?project_id=...` 只能看到本项目；无项目请求只看到 global Skill；
6. 聊天 `learning_context`、Skill matcher、execution plan 和 run receipt 均使用同一项目范围；
7. UI 的列表、同步、安装、审核、激活和删除请求均携带当前 `activeProjectId`。

## 具体变更

- `skills` 表新增 `scope/project_id` 及索引；旧表启动时向后兼容迁移为 global。
- Skill 入库、更新和 learning chunk 同时写入 scope，行字段在解密 payload 后覆盖不可信同名字段。
- `sync_project_skills()` 同步 workspace global 根和当前项目根，重复检测按 `(project_id, source_path)` 而不是单独按路径。
- 管理读取、删除、归档、运行记录和执行计划增加项目范围；运行幂等摘要包含 `project_id`，避免同键跨项目错误回放。
- `/learning/skills`、`/sync`、`/inbox`、`match`、`runs` 以及 Skill run/PPT 风格接口增加项目范围传播。
- `PresentationService` 对旧的一参数 style lookup 保持兼容，同时支持把 PPT 的 `project_id` 传到 Skill 解析边界。
- UI 切换项目后刷新项目 Skill 命名空间，保留全局 Skill 的共享语义。

## 验收与边界

已通过的定向证据：

- LearningStore 项目同路径隔离：`3 passed`（包含修复前失败反例）；
- 项目 Skill 发现/项目根同步：`4 passed`；
- MCP 项目聊天隔离与 HTTP Skill namespace：各定向用例通过；
- MCP 学习回归：`112 passed, 1 warning`；
- Skill/PPT/LearningStore 组合：`122 passed`；
- UI Skill 回归：`4 passed`；生产构建通过。

尚未宣称：

- 不自动把任意 workspace 文件猜成项目 Skill；项目级源目录约定是 `<project_root>/skills/`；
- 不提供 Skill 内容的模型答案准确率、自动 procedural memory 抽取质量或跨模型泛化结论；
- 不引入 Graphiti/Letta/LangGraph 运行时、远程 namespace 服务或图数据库；
- 不把 Skill namespace 等同于完整 Agentic RAG、DRIFT 或外部 exactly-once；
- 全量门禁结果以本报告更新记录为准，`scripts/check.ps1` 仍不证明真实 Tauri/WPS UI、打包启动和生产外部服务。
