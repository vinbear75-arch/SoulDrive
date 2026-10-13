# 研究窗口评测契约与分层指标优化

日期：2026-09-13

## 发现

现有 RAG 运行时已经把 `workflow_id` 传入检索焦点、服务端提示、引用命名空间和输出章节契约，但离线评测脚本没有同步这个身份：`EvalRecord`/`EvalResult` 不保存窗口，结果文件只按样本 ID 对齐，检索评测也没有复用窗口专属的 retrieval focus。这样一来，论文精读、实验对比和会议行动项可以在结果层面被当成同一种任务，无法证明不同窗口确实承担不同职责。

## 公开实践对照

- [Ragas 可用指标](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/) 将 Context Precision、Context Recall、Faithfulness 等拆开；本地继续区分来源命中/覆盖、位置敏感的 `context_precision`、引用支撑和窗口输出契约，不把一个综合分数当成答案事实正确率。
- [DeepEval Faithfulness](https://deepeval.com/docs/metrics-faithfulness) 将回答中的 claim 与检索上下文分别检查；本地已有引用合法性和 claim 支撑门，本轮把窗口契约作为另一条独立维度。
- [Langfuse 数据集实验](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/evaluation/experiments/datasets.mdx) 强调数据集样本、元数据和实验结果需要稳定对齐；本地把已注册的 `workflow_id` 纳入数据集/结果的 fail-closed 对齐校验，避免跨窗口结果复用。

## 实现

1. `scripts/eval_rag.py` 的 `EvalRecord`、`EvalResult` 和 JSONL 保存格式新增可选 `workflow_id`。
2. 数据集与结果读取只接受 `paper_review`、`experiment_compare`、`meeting_actions`；窗口缺失、错配或未知值会在评分前拒绝，旧的无窗口通用评测仍保持兼容。
3. 检索评测复用现有 `workflow_retrieval_variants()`，把论文/实验窗口的有限 focus variants 传给 `LocalKnowledgeBase`；原始用户问题仍是主 query，没有把 Playbook 控制文本拼入问题。
4. 答案生成评测将 `workflow_id` 放入结构化 `interaction_context`；模型失败时的本地证据回退也保留窗口 focus 和输出章节契约。
5. 评分新增 `workflow_output_contract` 与 `workflow_contract_valid`，汇总输出总的窗口契约通过率及按窗口分组的样本数、检索任一命中率和契约通过率。该指标只验证角色输出结构，不冒充事实正确率。

## 失败优先与验收

- 新增回归第一次运行确实失败：5 个窗口评测用例中，旧实现出现 `EvalRecord` 不接受 `workflow_id`、未知窗口不报错、答案回退不接受窗口上下文等失败。
- 实现后窗口专项 `5 passed`；完整评测资产测试 `50 passed`；评测、工作流和 RAG 提示组合 `170 passed, 1 warning`。
- 随后完整 `scripts/check.ps1` 通过：后端 `1219 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界与下一步

- 当前正式企业评测集没有凭空添加窗口标签，因此本轮没有宣称三类窗口的业务质量数值提升；新增能力是让后续冻结的窗口评测集能够安全、可分组、可复现地运行。
- `workflow_output_contract_pass_rate` 只说明标题/章节和窗口角色契约是否满足；它不等于 Ragas Faithfulness、答案事实准确率、用户满意度或模型 judge 分数。
- 仍未引入自由循环 Agentic RAG。只有在同一冻结数据、同一索引/模型、同一延迟预算下，分别比较 text-only、当前有界检索和有限证据工具循环，并证明留出集收益后，才重新评估是否增加工具循环。
- 后续若要产生三类窗口的业务数值，先冻结每个窗口的最小标注集、来源/文档身份、项目与时间范围，再运行 B0/B1/候选；不得用本轮契约回归替代业务答案质量评测。
