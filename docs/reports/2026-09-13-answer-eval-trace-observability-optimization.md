# 答案评测 trace 可观测性优化

日期：2026-09-13

## 目标

前一轮已经把“证据是否真的回答问题”和“已完成拒答不可被回退覆盖”收紧，但仅有最终文本仍不足以判断答案是正常完成、受控拒答还是异常回退。本轮把最小的检索决策 trace 写入每条答案评测结果，便于逐样本审计路由和终态。

## 外部实践对照

- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md) 将模型调用、输入输出和状态作为可观测事件；本地评测采用同样的“结构化事件优先”思路，但只保存当前评测所需的决策字段。
- [Langfuse observation types](https://langfuse.com/docs/observability/features/observation-types) 区分 generation、retrieval 等观察类型；本轮把 `retrieval_observability` 与答案文本并列保存，而不是把路由信息埋在日志字符串中。
- [Restate durable execution](https://docs.restate.dev/foundations/key-concepts) 强调可恢复执行的状态边界；本轮把 `refuse`、`fallback` 和正常完成区分为可检查的终态，避免后续评测辅助逻辑把拒答重新解释成成功答案。

## 实现

- `scripts/eval_rag.py::run_answer_generation` 为每条评测结果收集 `schema_version=agentic_retrieval_trace_v1` 的最小 trace。
- 正常证据问答记录主检索完成状态、路由决策和停止原因；主链形成拒答且没有证据块时记录 `agentic_decision=refuse`，不再触发裸证据回退；运行时异常才记录 `agentic_decision=fallback`。
- trace 不保存完整提示词、模型密钥或整段私密上下文，只保存逐样本审计所需的状态字段，并与现有引用/支持性指标分开。
- 新增 `tests/test_eval_assets.py::test_answer_fallback_can_capture_retrieval_trace`，验证回退结果能被观察且拒答终态不会被改写。

## 结果

- 拒答集 `eval/results/rag_cn_raw_v0.1_final_refusal_trace_20260913.jsonl`：4 条，拒答正确率 `100%`，漏放 `0%`；4 条均有 trace，3 条走受控 `refuse`，1 条为完成拒答。
- 开发集 `eval/results/rag_cn_raw_v0.1_final_dev_trace_answers_20260913.jsonl`：16 条，trace 缺失 `0`，正常完成/受控回退状态与结果一致；检索、引用、支持性、综合可靠率和关键词覆盖率均为 `100%`，不支持 claim `0`，误拒 `0`。
- sealed 集 `eval/results/rag_cn_raw_v0.1_final_sealed_trace_answers_20260913.jsonl`：8 条，trace 缺失 `0`，上述答案自动门均为 `100%`，不支持 claim `0`，误拒 `0`。
- 相关回归 `tests/test_eval_assets.py tests/test_secure_retrieval_flow.py`：`78 passed`；随后完整 `scripts/check.ps1` 后端 `1351 passed, 1 warning`，前端构建及 8 组 UI 测试 `61 passed`，`compileall` 与 `cargo check --locked` 通过。

## 边界

- 这是本地冻结语料、当前量化模型和评测辅助链路的可观测性证据，不等于已经接入 OpenTelemetry/Langfuse 后端，也不等于真实 Tauri/WPS UI、流式断线恢复或跨进程 exactly-once 已验收。
- trace 的存在和状态一致性不能证明答案事实正确；事实仍需引用定位、逐 claim 支持性检查和人工/业务评审。
- 当前只记录最小检索决策，不把尚未实现的多轮 Agentic RAG、外部工具副作用或自动补偿写成已完成能力。
