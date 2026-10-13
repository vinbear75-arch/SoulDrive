# SoulDrive 记忆事实时间有效性优化

## 结论

本轮为现有加密 SQLite 记忆层补上了一个受限但可验证的时间契约：`valid_from` 表示事实开始生效的 Unix 时间，`invalid_at` 表示事实失效时间；当前学习/RAG 上下文只读取当前有效事实，学习搜索可以显式使用 `as_of` 查询历史时点。

这不是完整的 Temporal GraphRAG，也没有让模型自动猜测日期或自动覆盖旧事实。

## 外部实践对齐

- Graphiti/Zep 将 episode 作为摄取事件并保留来源，使知识可以按时间点查询并追踪 provenance：[Adding Episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes)。
- Zep Facts 为事实保存 `valid_at`、`invalid_at`、`expired_at`，新事实可以使旧事实失效，同时保留历史：[Facts](https://help.getzep.com/facts)。
- Mem0 的更新接口允许在修改记忆时同步修改元数据、时间戳和过期日期，并要求更新后重新验证检索结果：[Update Memory](https://docs.mem0.ai/core-concepts/memory-operations/update)。

SoulDrive 当前只有本机单用户、人工审核的长期记忆，因此先实现显式时间窗口和查询过滤，不引入 Neo4j/Graphiti 或新的向量服务。

## 当前缺口与改动

原有记忆只有创建、修改、最近使用和活动/归档状态。旧事实可以被编辑或撤销，但无法表达“未来才生效”或“在某个时间点失效”，导致历史回溯和当前上下文之间没有明确边界。

本轮改动：

- `memories` 与 `learning_chunks` 增加 `valid_from`、`invalid_at`，并为旧数据库提供可重复迁移。
- `LearningStore.update_memory` 支持显式设置、清除和校验有效区间；`invalid_at <= valid_from` 会拒绝。
- `context_only` 列表和搜索默认以当前时间过滤未来/失效记忆；普通管理列表仍能看到记录及其有效性字段，便于恢复或编辑。
- `/learning/search` 增加可选 `as_of`，用于确定性历史检索。
- 当前记忆仍先经过项目范围过滤、敏感内容门、相关性门、冲突门和字符预算；时间过滤不是权限替代品。

## 验收证据

- 先添加失败回归：未来事实、已失效事实和 `as_of` 历史查询均无法调用尚不存在的接口。
- 修正后定向回归：`tests/test_learning_store.py tests/test_mcp_server_learning.py -k "memory_validity or memory_edit_updates or memory_management_exposes"`，`4 passed, 1 warning`。
- 该改动后的仓库全量回归已执行：当时后端 `1001 passed, 1 warning`；后续时间路由、记忆版本历史、工作流文档范围、TaskJournal 中断 fencing、邮件预留竞态、长步骤恢复保护、记忆审核并发、自动记忆写入边界、审核草稿项目绑定和自动记忆候选范围修正后的最新结果为后端 `1019 passed, 1 warning`，前端 `44 passed`，Tauri `cargo check --locked` 通过。

## 明确限制

- 本报告这一轮的日期必须由用户/API 显式提供；后续聊天时点路由已增加受限的历史短语解析，但仍没有自动判断“新事实是否覆盖旧事实”。
- `as_of` 是学习搜索接口能力；普通 `/chat` 后续已对单一明确日期、`上个月`、`上周`、`去年`做受限解析，但“某次实验”的实体选择和多日期比较仍需显式输入或澄清。
- 还没有 episode 图、实体级时间关系、多源冲突合并或真实模型质量提升证据。
