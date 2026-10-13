# 2026-09-13 Chat 失败审计错误指纹优化

## 结论

`chat.failed` 原先把异常原文写入审计 payload。普通审计文件会做路径和敏感字段处理，但异常消息仍可能包含供应商回显、用户片段或内部实现细节；同一原文还会进入公开运行状态的 `last_error`。现在失败审计和 Chat 运行指标只保存异常类型、异常长度和 16 位 SHA-256 指纹；同类异常仍可通过指纹关联，原文不进入这两条观测路径。

## 外部实践对照

- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md) 将错误状态、错误类型和观测属性作为追踪字段；本地实现采用同样的“结构化状态优先”方向，但不接入远程 trace 后端。
- [Langfuse observation types](https://langfuse.com/docs/observability/features/observation-types) 将 observation 元数据与生成内容区分；本轮保留 `retrieval_trace` 的结构化状态，不把异常正文和查询正文塞进失败观测。

## 失败优先反例

新增 `tests/test_mcp_server_learning.py::McpServerLearningTests::test_chat_failure_audit_uses_error_fingerprint_not_raw_exception_text`：让 RAG 抛出包含 `secret-user-fragment` 的异常，并检查 `chat.failed` 审计 payload。不修复时异常正文会直接出现；修复后正文不存在，`error_type=RuntimeError`，`error_digest` 满足固定十六进制格式。

## 实现范围

- `core/mcp_server.py` 新增 `_chat_error_observation()`，`chat.failed` 和 Chat 失败的 `RuntimeMetrics.last_error` 使用错误类型、长度和摘要替代 `str(exc)`。
- 不改变客户端对外的通用失败文案，不改变本地 `RuntimeMetrics.last_error` 的既有路径/敏感字段清理契约，不改变加密 Skill 运行记录中的失败原因。

## 验证

- 断线、生产 retrieval trace、失败错误指纹定向回归：`3 passed, 1 warning`；上一轮 Chat/收据/运行指标组合为 `28 passed, 1 warning`。
- 修改后的完整 `scripts/check.ps1`：后端 `1355 passed, 1 warning`；前端构建及 8 组 UI 测试 `61 passed`；`compileall` 与 `cargo check --locked` 通过。Vite 仍提示单个 JS chunk 超过 500 kB。

## 边界

- 指纹用于关联，不可反推出异常原文；本地加密运行记录仍可能保存更详细的失败原因，访问边界仍由工作区密钥和权限控制。
- 该修复不证明 OpenTelemetry/Langfuse 后端、人工答案准确率、真实 Tauri/WPS UI/E2E 或外部 exactly-once。
