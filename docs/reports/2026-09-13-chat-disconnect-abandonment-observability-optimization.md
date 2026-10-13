# 2026-09-13 Chat 流断线 abandoned 观测优化

## 结论

本轮只处理一个可复现的生产边界：同步 `StreamingResponse` 的生成器被关闭时，`GeneratorExit` 不会进入普通 `Exception` 分支；此前既没有审计事件，也没有运行指标。直接释放请求收据也不安全，因为同一请求的 RAG 工作线程可能仍在运行，过早释放会允许同一幂等键并发进入模型生成。

当前实现增加 `chat.abandoned` 审计事件和 `chat_abandoned` 运行计数，并在前端运行诊断面板显示“流式断线”。断线时保留仍在生效的请求租约，等待既有租约恢复机制处理；不把断线伪装成可立即重试，也不记录查询正文或生成正文。已完成的幂等回放在最后一次输出关闭时不会重复计作 abandoned。

## 外部实现对照

- [Starlette StreamingResponse](https://starlette.dev/responses/) 说明流式响应由生成器提供；连接生命周期由响应层负责。
- [Restate request lifecycle](https://docs.restate.dev/guides/request-lifecycle) 将客户端连接与持久化执行分开，连接丢失不等于执行已经安全停止；重试依赖已记录的执行进度。
- [Temporal activity execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx) 说明取消需要由活动感知并完成资源清理，不能仅凭外层连接关闭就假设工作已经终止。

这些实践支持本项目的保守选择：先增加可观测性并保留租约；当前本机 RAG 生成器没有统一的取消 token/heartbeat 契约，因此本轮不强行杀线程、不提前 `fail_request`。

## 失败优先反例

新增 `tests/test_mcp_server_learning.py::McpServerLearningTests::test_chat_disconnect_is_audited_without_releasing_active_receipt`：

1. 创建带 `session_id + request_id` 的 Chat 请求并领取本机请求收据。
2. 让 RAG 只输出第一段，直接关闭流式生成器，模拟客户端中途断线。
3. 断线后要求存在一条 `chat.abandoned`，原因是 `client_disconnected`，且请求收据仍为 `in_flight`。

测试最初暴露了运行指标 schema 未声明 `chat_abandoned` 的错误；补齐 `RuntimeMetrics` 字段和快照后通过。

## 实现范围

- `core/mcp_server.py`
  - 单独捕获 `GeneratorExit`。
  - 只在尚未完成持久化时记录 `chat.abandoned`。
  - 不释放可能仍被 RAG worker 使用的请求租约。
- `core/observability.py`
  - 增加 `chat_abandoned` 计数和运行状态快照字段。
- `souldrive-ui/src/App.tsx`
  - 增加可选运行指标类型和“流式断线”展示。

## 验证证据

- Chat/收据/运行指标定向回归：`28 passed, 1 warning`。
- 前端生产构建：通过；Vite 仍提示现有单个 JS chunk 超过 500 kB（约 `584.34 kB`，gzip 约 `178.71 kB`）。
- `git diff --check`：无实际 whitespace 错误；仅有 Windows 换行转换提示。
- 完整 `scripts/check.ps1`：后端 `1354 passed, 1 warning`；前端构建及 8 组 UI 测试 `61 passed`；`compileall` 与 `cargo check --locked` 通过。Vite 仍提示单个 JS chunk 超过 500 kB。

## 明确边界

- 本轮没有声称断线后能恢复已生成的流，也没有实现统一的模型取消 token、后台 durable run 或跨进程接管。
- 断线后仍运行的生成可能在租约恢复前继续消耗本地模型资源；若未来要缩短租约或立即失败，必须先让 RAG/模型调用提供可验证的取消与清理契约。
- 该改动不证明真实 Tauri/WPS UI、Windows 网络断线、安装包启动、OpenTelemetry/Langfuse 后端或外部 exactly-once。
