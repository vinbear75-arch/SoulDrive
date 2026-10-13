# SoulDrive 研究窗口检索焦点路由优化

日期：2026-09-10

## 结论

本轮确认并修复了一个真实缺口：此前 `retrieval_policy.profile` 只进入审计记录，`paper_review`、`experiment_compare` 和普通文档查询在候选变体层面没有稳定的行为差异。现在，已绑定业务窗口且已选文档时，服务端会把该窗口的固定检索重点作为有限的额外查询变体送入真实知识库搜索；原始用户问题仍是证据门和最终回答的唯一问题契约。

因此：论文精读会优先覆盖研究问题/方法与结果/复现，实验对比会优先覆盖指标/数据集/版本/配置/验证实验，会议纪要会优先覆盖决策与行动项。它们不是把业务说明拼进用户问题，也不是允许模型自由规划；每个窗口的重点由服务端 Playbook 固定、数量有界、可在审计中追踪。

## 外部实践对照

- [Stanford STORM](https://github.com/stanford-oval/storm) 将研究型写作拆为先研究与生成提纲、再写作，并用多视角问题驱动资料收集。本项目抽取的是“窗口应有明确问题焦点”的机制，不引入其联网研究运行时。
- [Haystack Pipelines](https://docs.haystack.deepset.ai/docs/pipelines) 与 [ConditionalRouter](https://docs.haystack.deepset.ai/docs/conditionalrouter) 把组件/条件路由作为实际 pipeline 行为，而不是只有标签。本轮据此检查到当前 profile 没有改变检索输入的缺口。
- [Microsoft GraphRAG global search](https://github.com/microsoft/graphrag/blob/main/docs/query/global_search.md) 说明整体主题问题需要专门的检索路径和分阶段上下文。本项目仍保持本机小型链路，只使用轻量、确定性的窗口焦点变体，不引入 GraphRAG 的额外运行时。

## 先失败后修复

新增两条失败优先回归：

- `tests/test_multimodal_workflows.py::test_workflow_retrieval_variants_make_research_windows_behaviorally_distinct`
- `tests/test_rag_prompting.py::RagPromptingTests::test_paper_review_window_reaches_search_with_research_focus_variants`

修复前第一条回归在模块导入阶段失败，项目没有窗口检索变体 API；第二条无法证明论文窗口的焦点进入知识库搜索。修复后两条均通过，并确认原始查询仍保持为检索主查询。

## 实现范围

- `core/multimodal_workflows.py`
  - 为三个窗口补充服务端固定 `query_focuses`。
  - 新增 `workflow_retrieval_variants()`，对查询重点做去重、截断和最多三条的确定性限制。
- `core/rag_engine.py`
  - 仅当存在已选文档时，把窗口焦点加入真实 `extra_query_variants`；显式检索反馈优先，窗口焦点最多填充剩余预算。
  - `rag.retrieval_planned` 与 `agentic_retrieval_trace_v1` 记录实际采用的窗口变体数量。
  - 原始查询继续用于证据门、引用校验和生成，避免把控制性说明伪装成用户证据问题。

## 验证证据

- 新增窗口路由定向回归：`2 passed, 1 warning`。
- 工作流模块回归：`29 passed, 1 warning`。
- RAG、上下文门、查询扩展组合回归：`107 passed`。
- 本轮统一 `scripts/check.ps1`：后端 `1138 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组 UI 测试共 `48 passed`；`cargo check --locked` 通过。真实 Tauri/UI、真实模型答案质量和冻结留出集指标仍不由这些单元回归代替。

## 边界

- 检索焦点只是召回补充，不证明答案质量提升；必须在冻结数据集上比较 Recall/MRR/nDCG、证据覆盖、串项目率、延迟与显存。
- 当前没有引入 STORM 式联网研究、自由循环 Agentic RAG 或新的重量级依赖。
- “论文精读”仍要求当前项目中至少一篇已索引论文；“实验对比”仍由双来源/可比性门决定是否允许生成对比结论。
