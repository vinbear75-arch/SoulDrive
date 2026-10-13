# 2026-09-15 RAG 检索失败轨迹优化

## 结论

本轮修复了 Agentic RAG 可观测链路中的一个真实断点：首次检索异常虽然已经被 `RAGEngine` 转成固定的用户提示，但支持回调的生产 `/chat` 只会收到 `chat.failed`，不会收到本轮检索已经失败的结构化轨迹。现在首次检索失败会发出 `agentic_retrieval_trace_v1`，明确记录失败阶段、终止原因、证据数、错误类型和摘要指纹；异常正文仍不会进入回答或审计。

这不是检索质量提升，也不是引入新的多工具 Agent。它是 Agentic RAG 的失败终态补全，使“检索未执行、检索失败、证据拒答、生成失败”可以被区分并用于后续重试/诊断。

## 外部实践对照

- [OpenTelemetry Recording errors](https://opentelemetry.io/docs/specs/semconv/general/recording-errors/) 将异常视为结构化 operation outcome，并要求错误状态与低基数 `error.type` 分开记录；本轮沿用这一边界，只写类型、字符数和固定长度摘要，不写异常消息正文。
- [OpenTelemetry HTTP spans](https://opentelemetry.io/docs/specs/semconv/http/http-spans/) 要求失败操作设置可预测的 `error.type`，同时区分成功、取消和真正错误；本轮把初始检索失败标为 `status=failed` / `agentic_decision=error`，没有把它伪装成证据拒答。
- 本项目继续复用已有的 bounded trace 回调，不引入 OpenTelemetry SDK、Langfuse 或重量级 Agent runtime；这只借鉴错误观测的字段边界。

## 失败优先证据

新增 `McpServerLearningTests.test_chat_failure_audit_includes_retrieval_failure_trace` 后，先用支持回调但在检索前抛错的 RAG 替身验证生产入口合同；随后在 `RagPromptingTests.test_retrieval_failure_callback_reports_structured_failure_trace` 中直接调用真实 `RAGEngine` 路径。修复前真实失败表现为：回答仍是稳定的“本地知识库暂未就绪”提示，但 `chat.failed.retrieval_trace` 为 `None`，无法知道失败发生在初始检索。

## 最小实现

- `core/rag_engine.py` 的首次检索异常分支在固定提示前调用 `on_retrieval_completed`。
- 失败轨迹固定包含：`schema_version`、`status=failed`、`agentic_decision=error`、`stop_reason=retrieval_failed`、`failure_stage=initial_retrieval`、`evidence_count=0`、`retrieval_mode=unavailable`、Agentic 预算和 `error_type/error_digest/error_chars`。
- 回调本身采用 best-effort；如果审计/观测回调失败，仍返回稳定用户提示，不让可观测性故障改变业务错误边界。
- `core/mcp_server.py` 无需新增协议：现有 `/chat` 回调收集逻辑会把失败轨迹附加到 `chat.failed`，旧版不支持回调的插件仍保留兼容行为。

## 验证证据

- 失败优先修复后：RAG 检索失败与 Chat trace 定向 `2 passed`；RAG 提示词 + Chat/学习组合 `244 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1659 passed, 1 warning`；前端生产构建成功，UI 脚本 `66 passed`；`compileall` 和锁定 Cargo 检查通过。
- 冻结记忆评测：[memory_quality_after_retrieval_failure_trace_20260915.json](../../eval/results/memory_quality_after_retrieval_failure_trace_20260915.json)：`7/7` 场景、`8/8` 查询、Recall@K `1.0`、陈旧/越权泄漏率 `0.0`。
- GraphRAG 路由：[graphrag_route_after_retrieval_failure_trace_20260915.json](../../eval/results/graphrag_route_after_retrieval_failure_trace_20260915.json)：`4/4`。
- 研究窗口角色：[workflow_roles_after_retrieval_failure_trace_20260915.json](../../eval/results/workflow_roles_after_retrieval_failure_trace_20260915.json)：`6/6`。
- Learning loop：runtime `4/4`，静态 baseline `0/4`；本轮只改 RAG 失败观测，未改变记忆或 Skill 逻辑。

## 边界

本轮证明的是本机进程内 RAG 失败状态可追踪且不泄漏异常正文，不证明远程 trace 后端、跨进程/跨主机 exactly-once、断线恢复、模型事实准确率、真实 Redis/PostgreSQL 或真实 Tauri/WPS UI/E2E。真实冷工作区 GraphRAG 验收沿用此前已保存的固定结果，本轮未因观测字段变化重新构建 GraphRAG 索引。
