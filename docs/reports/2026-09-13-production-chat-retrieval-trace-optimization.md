# 生产 Chat 检索决策 trace 接入

日期：2026-09-13

## 发现的问题

评测脚本已经收集 `agentic_retrieval_trace_v1`，但真实 `/chat` 入口没有向 `RAGEngine.generate_response_stream()` 传递 `on_retrieval_completed`。因此生产会话只能看到分散的检索/完成事件，不能在同一条 chat 完成或失败审计中确认本轮是正常生成、拒答、检索修正还是异常回退。

## 外部实践对照

- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md) 建议把生成与检索状态作为结构化观测，而不是依赖不可解析的日志文本。
- [Temporal retry policies](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx) 将 Activity 的执行状态、重试和幂等边界分开；本地 Chat 采用同样的最小状态记录方式，不引入远程工作流服务。

## 实现

- `core/mcp_server.py` 在支持回调的生产 RAG 路径收集 `on_retrieval_completed`，并将 trace 附加到 `chat.completed` / `chat.failed` 审计事件。
- 只复用已有 `agentic_retrieval_trace_v1` 回调，不改变检索排序、重试预算、拒答门或生成文本；旧的测试替身/旧插件不支持回调时继续走兼容分支。
- trace 仍由 RAG 引擎控制字段范围，生产入口不把整段提示词、模型密钥或原始私密上下文写入聊天正文。
- 新增入口测试 `test_chat_audit_includes_agentic_retrieval_trace`，用支持回调的 RAG 替身验证真实 `/chat` 审计收到生成决策和证据数量。

## 结果

- 入口级 trace 回归：`3 passed`。
- Chat、学习、项目范围、检索流和会话持久化组合回归：`168 passed, 1 warning`。
- 代码变更后的完整 `scripts/check.ps1`：后端 `1353 passed, 1 warning`，前端构建及 8 组 UI 测试 `61 passed`，`compileall` 与 `cargo check --locked` 通过。

## 边界

- 这是本机 Chat 审计可追踪性增强，不等于已接入 OpenTelemetry/Langfuse 后端，也不等于流式断线取消、跨进程 exactly-once 或外部工具副作用已经具备远程 durable execution 语义。
- trace 记录检索决策和门控状态，不能替代引用支持性、人工事实评审或真实 Tauri/WPS UI/E2E。
