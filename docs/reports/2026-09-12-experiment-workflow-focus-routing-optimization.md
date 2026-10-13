# 实验对比窗口的结构化运行检索焦点优化

日期：2026-09-12

## 结论

实验对比 Playbook 现在不再把“已选文档”误当成进入实验专属检索焦点的唯一条件。只要会话已通过项目范围与可比性门，形成结构化实验测量 `M#`，即使用户只选择了基线运行和当前运行、没有 `document_names`，也会注入实验专属的“指标/数据集/评测脚本/版本/候选原因/验证实验”检索变体。

论文精读仍要求已选文档后才产生论文焦点；会议纪要仍以本轮确认的语音/转写/文本记录为主，不因本轮改动被强行路由到文献检索。

## 外部实践对照

- [Microsoft GraphRAG Query Overview](https://github.com/microsoft/graphrag/blob/main/docs/query/overview.md) 将 Local、Global、DRIFT、Basic 等查询方法按问题类型区分，而不是把查询路由绑定到某一种输入文件形态。
- [Microsoft GraphRAG Local Search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md) 将实体、图关系、社区报告和原始文本块组合为查询时上下文，说明结构化关系信号可以独立成为检索入口。
- [LangGraph Workflows and Agents](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/workflows-agents.mdx) 使用显式条件路由把不同任务送入不同节点；本地实现沿用这一原则，但只增加一个确定性条件，不引入通用工作流依赖。

这些项目只作为机制参考。SoulDrive 仍保持本机单用户、项目优先、证据门和有界检索预算。

## 失败优先验证

新增 `tests/test_rag_prompting.py::RagPromptingTests::test_experiment_compare_runs_use_focus_variants_without_selected_documents`：

- 输入只有当前项目、通过可比性门的基线/当前运行和 `M1` 测量，没有任何 `document_names`。
- 修复前，`engine.kb.extra_query_variants` 不包含实验专属的“指标/数据集”焦点，证明运行选择只改变了结构化证据，却没有完整改变检索行为。
- 修复后，检索审计的 `workflow_retrieval_variant_count >= 1`，同时回答继续使用 `M1`，没有改变原始用户问题或证据校验契约。

## 实现范围

- `core/rag_engine.py`：实验测量已授权时，与选中文档一样允许生成当前 Playbook 的检索焦点变体。
- 未修改工作流 catalog、结构化实验可比性门、项目过滤、来源多样性门、引用命名空间或外部副作用。

## 回归证据

- RAG 提示词、三类多模态工作流和交互上下文专项：`145 passed, 1 warning`。
- 新增用例与实验测量、论文精读相邻回归：`3 passed`。
- 全量 `scripts/check.ps1`：后端 `1156 passed, 1 warning`，Python compileall 通过；前端生产构建通过，7 组前端测试共 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮证明的是结构化运行输入会进入正确的检索焦点和审计路径，不证明检索 Recall、MRR、回答事实正确率或因果分析质量提升；仍需冻结业务评测集比较“有/无实验焦点变体”。这也不是 Agent 自由循环或 GraphRAG 全局/局部查询实现。
