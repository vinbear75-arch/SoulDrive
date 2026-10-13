# 研究窗口角色质量门优化

日期：2026-09-13

## 发现

SoulDrive 之前已经把 `workflow_id` 持久化到会话、记忆和 RAG 运行上下文，但三类研究窗口的差异主要分散在 `WORKFLOW_CATALOG`、提示词回归和接口测试中，缺少一个能同时检查“窗口是否真的不同”和“缺输入时是否拒绝”的冻结质量门。仅检查提示词文本，不能证明窗口的输入边界、检索重点、RAG 开关、输出结构和证据类型已经分开。

## 公开实践对照

- [LangGraph Graph API](https://langchain-ai.github.io/langgraph/how-tos/state-reducers/) 将工作流拆成共享状态、节点和边，并要求条件边显式决定下一步；本地沿用这个思想，但用确定性规划器和既有服务链路实现，不新增通用图执行框架。
- [LangGraph Workflows and Agents](https://langchain-ai.github.io/langgraph/agents/tools/) 展示了生成、校验、重试/结束等可观察分支；本地将“输入门、检索契约、输出契约”作为可测分支，仍保留有界重试，不引入自由循环。
- [LangMem 长期记忆概念](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) 区分 semantic、episodic 和 procedural memory；本地把研究窗口当作 procedural 任务约束，把项目/工作流事实作为带范围的记忆，不让一类窗口的事实污染另一类窗口。

## 实现

1. 新增 `scripts/eval_workflow_roles.py`，直接调用真实的 `plan_workflow()`、`workflow_retrieval_policy()`、`workflow_retrieval_variants()`、输出章节校验和引用命名空间校验，不调用模型、不读取真实用户工作区。
2. 新增 `eval/workflow_role_quality_v0.1.jsonl`，冻结 6 个最小样本：论文精读、实验对比、会议行动各包含一个可执行样本和一个缺输入拒绝样本。
3. 评测分别检查：
   - `paper_review` 是否只在有论文时就绪，检索 profile 为 `paper_review`，重点覆盖研究问题/实验结果，输出为论文专用五段，引用只允许 `E/V`；
   - `experiment_compare` 是否要求两份材料或等价可比输入，检索 profile 为 `experiment_compare`，要求来源多样性，输出区分已证实变化、候选原因和验证实验，引用允许 `E/V/M`；
   - `meeting_actions` 是否要求用户提供语音/转写/记录文本，profile 为 `meeting_record_first`，没有文档时不强行进入 RAG，输出为会议结论/行动项/待确认/草稿边界，引用允许 `A/E/V`。
4. 汇总同时报告角色、就绪门、检索 profile、检索 focus、RAG 开关、来源多样性、输出契约和引用命名空间，避免用一个总分掩盖某个窗口的失效。

## 失败优先与验收

- 新回归第一次运行在收集阶段真实失败：`scripts.eval_workflow_roles` 尚不存在，pytest 报 `ImportError`。
- 实现后专项 `tests/test_workflow_role_eval.py` 为 `2 passed`；真实冻结数据集为 `6/6`，以下契约全部 `1.0`：角色、就绪门、检索 profile、检索 focus、输出章节、引用命名空间、RAG 开关和来源多样性。
- 论文、实验、会议三组各自 `2/2` 通过；缺论文、缺第二份实验材料、缺会议记录的样本均按预期拒绝。
- 工作流/RAG/角色定向回归为 `122 passed, 1 warning`。
- 完整 `scripts/check.ps1` 通过：后端 `1228 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界与下一步

- 这是研究窗口的行为契约质量门，不是答案事实准确率、Ragas Faithfulness、用户满意度或端到端业务收益；6 个样本也不是正式留出集。
- 当前正式企业 RAG 评测集仍未擅自补加窗口标签，因此没有宣称三类窗口的业务检索质量已经提升。要产生业务对比，仍需用户确认每类窗口的真实样本、文档身份、项目范围和标注规则，然后冻结独立集合。
- 没有因为公开项目提供更复杂的 workflow/Agentic RAG 就增加自由循环。当前窗口已经有持久身份、输入门、专属检索 focus、输出章节、证据命名空间和可审计指标；只有在相同数据、索引、模型和延迟预算下证明有界工具循环的留出集收益后，才考虑下一步。
- 真实 Tauri/WPS 窗口切换、录音转写、屏幕授权和跨进程恢复仍需人工场景验证；本轮自动门不替代 UI/E2E 证据。
