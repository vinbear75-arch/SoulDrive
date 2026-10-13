# 2026-09-13 GraphRAG 诊断错误指纹优化

## 结论

GraphRAG 索引失败状态会进入工作区运行状态，并由受保护的诊断接口读取。此前项目级失败和全局失败都可能保存异常原文；旧运行状态即使已经落盘，诊断接口也会把原文继续返回。本轮将这条链路统一为 `error_type`、`error_chars`、`error_digest`，不再把异常内容作为运行状态或诊断响应字段。

## 对照实践

- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md) 将错误作为结构化观测字段，而不是把生成内容和异常正文混入普通指标。
- [Langfuse observation types](https://langfuse.com/docs/observability/features/observation-types) 将 observation 的状态、错误和 trace 关联分开；本地单机版本只保留可关联的短摘要指纹。
- [Microsoft GraphRAG outputs](https://microsoft.github.io/graphrag/index/outputs/) 把索引产物和运行状态分开；SoulDrive 继续把图谱作为可重建派生索引，失败信息不应成为公开诊断正文。

## 失败优先发现

- 项目 GraphRAG 构建异常会写入 `project_indexes[].error`。
- 全局 GraphRAG 构建异常会写入顶层 `graphrag.error`，随后 `/diagnostics/graphrag` 原样透传。
- 旧版本已落盘的 `error` 字段仍需要兼容读取，否则只修新写入会留下历史泄漏路径。

## 本轮修改

- `core/indexer.py` 的项目级和全局 GraphRAG 失败观测改为 `error_type/error_chars/error_digest`。
- `core/mcp_server.py` 的诊断响应兼容旧 `error` 字段：只在本地计算指纹并返回结构化字段，不返回原文。
- GraphRAG 源快照/范围元数据不可用时，MCP 诊断保留 `status=unavailable`，不再把它误标成 `not_built` 或触发自动重建。
- 业务状态、`degraded/failed` 语义、失败项目 ID 和重建路径不变；异常原文仍只留在本地受控日志边界。

## 验收证据

- GraphRAG、索引器与项目诊断回归：`70 passed, 1 warning`。
- 失败反例含 `secret=graph-token`；索引运行状态和 `/diagnostics/graphrag` 响应均不含该文本，错误类型、字符数和 16 位摘要存在。
- 修改后的完整 `scripts/check.ps1`：后端 `1356 passed, 1 warning`；前端构建及 8 组 UI 测试 `61 passed`；`compileall` 与 `cargo check --locked` 通过。Vite 仍提示既有单个 JS chunk 超过 500 kB。

## 边界

- 本轮只覆盖 GraphRAG 运行状态和诊断响应，不宣称所有 API 验证错误、第三方日志、模型上下文或历史日志已完成统一脱敏。
- 摘要指纹用于关联，不代表错误内容可恢复，也不等于接入远程 OpenTelemetry/Langfuse 后端。
- 这不改变 GraphRAG 检索质量、社区报告事实性、Agentic RAG 路由或跨存储 exactly-once 语义。
