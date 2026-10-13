# Agentic RAG 终止决策与来源覆盖优化

## 结论

本轮没有引入新的 Agent 框架，而是修复 SoulDrive 现有有界 Agentic RAG 链路的两个可验证缺口：

1. 检索完成回调此前无论最终证据是否被拒绝，都可能返回 `status=pass`，上层无法区分“可生成”和“重试耗尽后拒答”。
2. 比较类问题在单一来源同时包含两方关键词、且局部分数较高时，证据门可能接受，随后绕过 `ContextGate` 的来源多样性 `retry` 决策进入模型生成。

现在最终检索轨迹会记录 `agentic_decision`、`stop_reason`、`correction_attempted`、`retry_used`、`passes_used`、`max_passes`、上下文门和证据门；比较问题若最终只有一个可区分来源，会在生成前拒绝。

## 外部实践对照

- [LangGraph Agentic RAG](https://langchain-ai.github.io/langgraph/tutorials/rag/langgraph_agentic_rag/) 将检索代理拆成“决定直接回答/调用检索工具”、文档相关性评分、无关时改写再检索、相关时进入回答的条件图；该结构要求每个节点的下一步和停止路径可观察。
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 强调持久化状态、可恢复执行和生产 trace 检查；本轮把相同的“最终路径可解释”要求落实到现有回调，而不是引入新的 checkpoint 框架。
- [Microsoft GraphRAG Query Overview](https://microsoft.github.io/graphrag/query/overview/) 区分 basic、local、global 和 DRIFT 查询；[Local Search](https://microsoft.github.io/graphrag/query/local_search/) 强调图结构导航与原始文本证据共同参与，并需保留细粒度来源。SoulDrive 继续让 GraphRAG 负责路由/导航、让原始切片负责最终引用，并将来源覆盖当作比较任务的硬约束。

本轮没有声称已有数据集上答案质量提升，也没有把一次有限纠错升级为自由循环 Agent。原因是尚无冻结的业务留出集证明更多循环在同一延迟、显存和安全边界下有效。

## 实施

### 最终检索轨迹

`core/rag_engine.py` 的 `on_retrieval_completed` 现在：

- 对视觉观察-only 路径返回 `not_applicable / visual_observation_only`；
- 对证据拒绝返回 `refused / evidence_rejected`；
- 对一次纠错仍不足返回 `refused / evidence_retry_exhausted`；
- 对来源覆盖不足返回 `refused / source_diversity_unmet`；
- 只有真正可以进入回答生成时才返回 `pass / accepted_for_generation`；
- 明确返回是否尝试纠错、是否采用重试结果、实际轮数和最大轮数，并带上最终 `ContextGate` 与 `EvidenceGate` 的公开结果。

回调结构版本为 `agentic_retrieval_trace_v1`。该结构只包含计数、决策、来源和门控结果，不复制文档正文。

### 比较任务的最终门

当查询被 `query_requires_source_diversity()` 判定为需要多来源，且最终上下文只有一个来源时：

- 若证据本身不足，继续沿用原有低证据拒答；
- 若证据分数高但仍只有一个来源，也拒绝生成比较结论；
- 仅有两方关键词出现在同一份文档中，不再被当作“两份可区分来源”；
- 已有实验对比窗口的专用拒答文案和事件类型保持兼容。

这修复的是“局部高分覆盖整体约束”的根因，不改变普通单文档事实查询的分数优先策略。

## 失败优先回归

新增 `tests/test_rag_prompting.py`：

- `test_retrieval_callback_reports_retry_exhaustion_instead_of_pass`：弱证据两轮均未达门时，确认不调用模型，轨迹为 `refused / evidence_retry_exhausted`，并报告 `passes_used=2`；
- `test_comparison_with_one_source_refuses_even_when_score_is_high`：单一高分来源同时提及甲乙方案时，确认不调用模型，轨迹为 `refused / source_diversity_unmet`。

## 验证

- 先失败后修复：上述两个回归在旧代码下分别出现伪 `pass` 和实际调用模型，修复后通过；
- RAG/GraphRAG/上下文门/答案质量/安全检索定向回归：`178 passed`；
- 全量 `scripts/check.ps1`：后端 `1110 passed, 1 warning`；Python compileall 通过；前端生产构建通过；前端 7 组测试共 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

- `agentic_retrieval_trace_v1` 是可观测性与终止门，不是新的多工具 Agent 循环；
- 来源多样性按现有查询意图和 `source_filename + project_id` 统计，不解决跨文档实体消歧；
- GraphRAG 社区报告仍是导航摘要，最终回答仍只能使用回读的原始切片；
- 没有冻结业务集、逐样本答案标签和统一延迟条件前，不宣称 Recall、MRR 或回答准确率提升；
- 真实 Tauri/WPS、在线模型/视觉/语音和安装包仍需单独人工/发行门验收。
