# 工作流级检索策略优化

## 结论

本轮把“研究窗口有不同作用”从输出模板扩展到检索行为：`experiment_compare` 现在有独立的检索策略标识，并在选中至少两份实验材料时要求最终文档证据至少来自两个不同来源。用户问题没有写“对比”或“两份”时，也不会因为漏写关键词而退化成单来源对比答案。

如果首轮来源不足，系统只进行一次现有有界纠错检索；纠错仍不足时，系统保留已检索证据、记录 `rag.refused_workflow_source_diversity`，并按实验对比 Playbook 输出“待确认”章节，不调用模型生成未经双来源支持的对比结论。论文精读、会议记录和普通查询不启用这条额外门控。

## 外部实践对照

- [Microsoft GraphRAG Query Engine](https://github.com/microsoft/graphrag/blob/main/docs/query/overview.md)：将 local、global、DRIFT、basic 等查询路径区分开，不把所有问题交给同一路径。
- [Microsoft GraphRAG Local Search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md)：结构化图信息与原始文本块在查询时分别召回、排序和过滤后再组装上下文；检索路径是查询策略的一部分。
- [Haystack MetadataRouter](https://github.com/deepset-ai/haystack/blob/main/haystack/components/routers/metadata_router.py)：使用文档 metadata 做显式路由，并保留 unmatched 分支；路由条件不是拼接到用户文本里的提示语。
- [LlamaIndex Workflow Client guidance](https://github.com/run-llama/llama_index/discussions/20561)：工作流运行时状态与客户端显式输入分开传递，客户端输入不应伪装成内部 Context。

本项目不引入 GraphRAG、Haystack 或 LlamaIndex 运行时，只抽取“按任务策略路由、按证据类型设门”的小型本地实现。

## 实施

- `core/multimodal_workflows.py`
  - 为三个 Playbook 增加结构化 `retrieval_policy`：论文精读、实验对比、会议记录各自有 profile；仅实验对比声明 `requires_source_diversity`。
  - 新增 `workflow_retrieval_policy`，返回副本，避免调用方修改目录。
- `core/context_gate.py`
  - `plan_retrieval` 支持由工作流显式要求来源多样性；保留社会寒暄的非检索短路。
  - 被强制要求时记录 `workflow_source_diversity` 原因并开启一次纠错预算。
- `core/rag_engine.py`
  - 根据结构化 workflow policy 和选中文档数量决定是否启用双来源约束。
  - 纠错重试的来源覆盖保护支持工作流强制模式；最终来源仍不足时 fail-closed，不进入模型生成。
  - `rag.retrieval_planned`、检索完成回调和拒答审计记录策略 profile、是否要求覆盖和是否仍缺来源。
- `tests/test_rag_prompting.py`
  - 新增回归：实验对比窗口选中两份材料，但用户 query 没有比较关键词且两次检索都只返回一个来源时，必须重试、拒答并且模型调用次数为零。

## 验证

- 先按旧行为运行新回归：单来源证据会进入模型并触发生成，证明窗口策略此前没有真正约束检索。
- 修复后工作流、上下文门和 RAG 定向回归：`120 passed, 1 warning`。
- 该阶段统一检查快照：后端 `1069 passed, 1 warning`；Python compileall 通过；前端构建通过，7 组 UI 测试 `48 passed`；Tauri `cargo check --locked` 通过。当前仓库最新全量证据见 `docs/reports/2026-09-10-workflow-focus-routing-optimization.md`。

## 边界

- 双来源约束只适用于“已选至少两份文档”的实验对比路径；明确授权屏幕表格或结构化实验测量的证据路径仍由各自的 V#/M# 门控负责。
- “两个来源”是文档来源覆盖，不代表两个来源内容可比，也不代表原因成立；指标定义、数据集、版本、配置和评测脚本仍必须通过实验台账与答案证据校验。
- 当前仍是一轮有限纠错，不是自由循环 Agentic RAG；是否提升答案质量需用冻结留出集和同条件延迟/显存指标验证。
