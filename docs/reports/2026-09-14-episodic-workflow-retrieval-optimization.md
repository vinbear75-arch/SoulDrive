# 情景活动进入工作流检索链路优化

日期：2026-09-14  
范围：`experiment_compare` / `meeting_actions` 会话窗口、项目历史投影、RAG 检索变体

## 结论

本轮修复了一个真实链路断点：情景活动此前只在 `/projects/{project_id}/overview` 展示，聊天生成虽然有会话角色和长期记忆，但不会读取项目历史活动。因此“回顾上次会议决策”“回顾上次实验运行”只能依赖文档检索，无法稳定利用已经存在的项目事实源。

现在的最小实现是只读、项目绑定、有界的活动定位上下文：

1. 继续复用会话 manifest、实验台账、项目决策和 TaskJournal，不新增 episodic 数据库。
2. 只有 `experiment_compare` 和 `meeting_actions` 可以读取活动定位；`paper_review` 明确不接入。
3. 查询必须包含明确的历史/时间表达，如“上次”“之前”“回顾”“最近”；普通的“把本轮记录整理为行动项”不会注入旧活动。
4. 事件必须属于当前 `project_id`；带其他 `workflow_id` 的事件不会进入当前窗口；结果最多 4 条，按来源相关性和原投影顺序稳定选择。
5. 活动摘要先进入受限检索变体，再进入生成提示；提示中标记为“仅用于定位，不是文档证据”，不能替代 [E#]/[A#]/[M#] 引用。
6. 审计记录新增活动数量、选择原因和 `view_digest`；RAG 计划记录新增 `episodic_retrieval_variant_count`。

## 公开开源实践对照

- [LangGraph Persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx) 将 thread/checkpoint 的短期状态与跨线程 Store 的长期数据分开。本地继续让 `session_id` 负责短期上下文，让项目活动事实源负责跨会话定位，不把二者合成一个无边界记忆池。
- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 为 episode 提供 `group_id`、事件发生时间 `reference_time`、来源类型和可回溯 episode 标识。本地借鉴项目范围、发生时间和来源引用，但不引入 Neo4j、实体抽取或双时间图。
- [Mem0 memory types](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-types.mdx) 明确说明其开源实现中 procedural memory 已接通，而 episodic/semantic 类型并没有自动通路。本地因此没有把现有活动投影误标成“完整 episodic memory”，只实现可审计的确定性路由提示。

## 失败优先与实现

先加入 `select_episodic_events_for_query()` 的工作流/时间隔离测试；旧实现直接在收集阶段失败：

```text
ImportError: cannot import name 'select_episodic_events_for_query'
```

随后完成：

- `core/episodic_view.py` 新增确定性选择器，按窗口允许的来源类型和查询时间标记选择活动。
- `core/mcp_server.py::_project_episodic_context_for_query()` 只读取当前项目的实验运行、决策、会话 manifest 和 TaskJournal 公共字段；不读取会话正文、任务结果或其他项目记录。
- `core/mcp_server.py::_learning_context_for_query()` 把结果作为独立 `episodic_context` 返回；投影失败时不影响长期记忆和 Skill 路径。
- `core/rag_engine.py` 将最多 2 条活动摘要形成额外检索变体，并写入检索计划审计；活动正文仍按不可信数据处理。
- `build_learning_context()` 将活动显示为“项目活动索引（仅用于定位，不是文档证据）”。

## 验收证据

- 情景投影与选择器：`tests/test_episodic_view.py`，`7 passed`。
- RAG/MCP/情景组合：`214 passed, 1 warning`。
- 新增检索链路反例：`tests/test_rag_prompting.py` 验证活动摘要确实进入 `extra_query_variants`，并断言 `episodic_retrieval_variant_count=1`。
- 冻结路由评测：
  - 数据集：`eval/episodic_workflow_routing_v0.1.jsonl`，4 个场景。
  - 无活动投影 baseline：`2/4`，`pass_rate=0.5`。
  - 运行时投影：`4/4`，`pass_rate=1.0`；会议历史、实验历史和论文窗口隔离均命中。
- 全量 `scripts/check.ps1` 已通过：后端 `1404 passed, 1 warning`；前端生产构建通过，8 组脚本测试合计 `61 passed`；`compileall` 与 `cargo check --locked` 通过。该门只能证明代码/构建回归，不证明真实模型答案准确率或 Tauri/WPS 端到端行为。

## 明确边界

1. 这仍是既有事实源上的只读定位投影，不是新的统一 episodic 存储、GraphRAG 图记忆或 Agentic RAG。
2. 活动定位提示不是引用证据；回答仍必须回到文档、用户确认记录或结构化测量证据。若只有活动索引而没有可引用证据，系统应保持待确认/拒答边界。
3. 本轮未把 `project_tasks` 表强行投影为事件，因为该表当前缺少可靠的创建/更新时间；用到期时间冒充发生时间会破坏时间语义。后续若要接入，应先补事实字段和迁移/回归。
4. 尚未声称实现 Graphiti 双时间模型、自动事件抽取、跨实体时间推理、Agentic RAG 或答案质量提升。
