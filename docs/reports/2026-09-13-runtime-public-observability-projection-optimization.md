# 2026-09-13 运行指标与文档索引观测公开投影优化

## 目标

关闭运行指标、`/runtime/status` 以及文档库索引状态之间的错误投影不一致：公开 UI 需要知道“失败/处理中/可重试”和文件名，但不需要收到 Redis 连接串、模型异常原文、绝对路径或 token。

## 外部实践对齐

- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai-spans.md)：把错误类型、关联属性和摘要作为结构化观测，避免将异常全文作为业务字段。
- [Langfuse observation types](https://langfuse.com/docs/observability/features/observation-types)：把观测状态和错误关联与用户可见结果分开，便于聚合诊断而不扩大数据暴露面。
- [Microsoft GraphRAG outputs](https://microsoft.github.io/graphrag/index/outputs/)：派生 GraphRAG 状态可被诊断和重建，不能成为原始错误或原始资产路径的旁路。

## 实际改动

1. `RuntimeMetrics.record_error` 将 `/metrics` 的 `last_error` 固定为用户提示，并新增 `last_error_observation`（`error_type/error_chars/error_digest`）；计数、延迟、失败率和 UI 状态不变。
2. 文档库索引失败投影按统一 `error_code` 映射固定原因，附加错误指纹；旧 `_bounded_indexing_reason` 不再作为文档公开响应的原文通道。
3. `/runtime/status` 对旧版嵌套 `indexing.observability.documents` 和 GraphRAG `project_indexes` 做兼容投影：文件名只保留 basename，旧 `error/reason` 转为指纹，已完成的状态和项目列表仍保留。

## 失败优先证据

- `/metrics` 反例包含不带字段名的 Redis URI、Windows 私有路径和 token；响应只返回固定提示和 16 位十六进制错误摘要。
- 文档列表反例在 `NO_PARSEABLE_CHUNKS` 的 reason 中加入连接串和 token；UI 仍得到“未提取到可用内容”、解析阶段和错误指纹，但看不到原文。
- 旧运行状态反例在文档观测和 GraphRAG 项目观测中加入异常原文；公开运行状态只保留安全文件名、状态和 `error_observation`。

## 验收证据

- 公开运行/文档定向回归：`4 passed, 1 warning`；指标/审计相关定向回归：`6 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1372 passed, 1 warning`；前端生产构建通过，8 组 UI 测试合计 `61 passed`；`compileall` 和 `cargo check --locked` 通过。

## 边界

这只证明公开投影和旧状态兼容读取的边界，不证明所有内部日志、所有参数校验错误、外部 observability backend、跨进程 exactly-once 或真实 Tauri/WPS UI/E2E 已完成。`/metrics` 仍是受保护接口；公开 `/health` 与 `/runtime/status` 的可用性字段也不等于底层服务高可用。
