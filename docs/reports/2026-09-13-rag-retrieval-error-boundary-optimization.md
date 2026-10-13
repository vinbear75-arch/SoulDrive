# 2026-09-13 RAG 检索失败错误边界优化

## 结论

RAG 检索异常此前会被 `generate_response_stream()` 捕获后直接拼进用户回答，并以 `error` 原文写入 `rag.retrieve_failed` / `rag.retrieval_retry_failed` 审计。这样即使 Chat 外层失败审计已指纹化，检索内部仍会把向量库、路径或连接串错误带到回答面。本轮改为固定可行动提示，审计与运行指标只保留错误类型、长度和摘要。

## 对照实践

- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai-spans.md) 把失败作为结构化观测属性；本地检索事件采用同样的 bounded error fields。
- [Langfuse observation types](https://langfuse.com/docs/observability/features/observation-types) 将 observation 状态和错误关联分离；检索失败不应把后端异常正文当作回答内容。
- [Starlette StreamingResponse](https://starlette.dev/responses/) 说明流式响应是传输边界；本轮沿用该边界，在检索阶段失败时输出稳定消息，不把内部异常串进 SSE 文本。

## 失败优先发现

- `rag.retrieve_failed` 审计保存 `str(exc)`。
- 检索失败分支向回答流 yield `本地知识库暂未就绪。错误信息：...`，异常可能包含用户路径、数据库连接串或供应商回显。
- Chat 外层的失败指纹修复不能覆盖“RAG 内部捕获后继续完成流”的路径，因此必须在 RAG 边界单独验证。

## 本轮修改

- 为 RAG 检索失败构造 `error_type/error_chars/error_digest`。
- 首次检索失败和有限重试失败的审计均移除 `error` 原文字段；运行指标记录类型+摘要关联值。
- 用户只看到“本地知识库暂未就绪，请稍后重试；如持续发生，请查看运行诊断。”，不改变检索失败计数或后续成功路径。

## 验收证据

- RAG 提示词、GraphRAG、生产 Chat/学习回归：`231 passed, 1 warning`。
- 失败反例包含 `secret=vector-token`；回答、审计事件均不含该文本，且审计含固定长度摘要。
- 修改后的完整 `scripts/check.ps1`：后端 `1362 passed, 1 warning`；前端构建及 8 组 UI 测试 `61 passed`；`compileall` 与 `cargo check --locked` 通过。Vite 仍提示既有单个 JS chunk 超过 500 kB。

## 边界

- 本轮覆盖 RAG 检索失败和一次有限纠错检索失败，不宣称所有模型生成异常、第三方日志或 API 验证错误已统一脱敏。
- 固定提示只表示检索暂不可用，不替代运行诊断；不改变项目范围、证据门、拒答门或 GraphRAG 路由。
- 这不等于检索质量提升、流式恢复、远程 trace 后端或外部 exactly-once。
