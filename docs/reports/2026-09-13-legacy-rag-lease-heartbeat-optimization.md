# 旧版 RAG 插件的 Chat durable 租约续期优化

日期：2026-09-13

## 发现

Chat 请求收据是本机至少一次执行边界，默认租约为 120 秒；当前 RAG 引擎通过队列等待和零宽 heartbeat 在长检索/生成期间续租。但兼容旧版或第三方 RAG 插件时，若其 `generate_response_stream` 签名不接收进度回调，旧路径会直接在请求生成器中调用插件。插件在首个 chunk 前长时间阻塞时，请求线程无法续租，租约过期后同一 `request_id` 可能被新的请求接管。

## 公开实践对照

- [Temporal Activity Execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx) 将长任务 heartbeat 视为执行存活信号，不能只在任务产出结果时更新状态。
- [Temporal retry policies](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx) 强调重试与至少一次执行之间的边界；本机 Chat 收据同样不能把租约过期误解为外部副作用已安全取消。
- [Starlette StreamingResponse](https://starlette.dev/responses/) 的流式响应可能在客户端断开时结束消费；后台工作和租约状态必须有明确边界，不能只依赖下一次输出。

## 实现

1. 当前 RAG 引擎和严格签名的旧版插件统一在后台 worker 中执行。
2. 请求线程统一从队列读取输出；队列 2 秒无消息时续租并发送零宽 heartbeat，heartbeat 不进入最终 assistant 内容。
3. 仅当插件支持回调时才注入 `on_retrieval_completed`；旧插件仍按原参数契约调用，避免为了续租破坏兼容性。
4. 断线时仍保持既有策略：记录 `chat.abandoned`，不主动释放可能仍在执行的请求收据；这不是取消后台模型的承诺。

## 失败优先与验收

- 新增严格旧版签名插件反例：插件在首个 chunk 前等待，修复前 `renewed_after_claim` 不会发生；修复后等待期间至少完成一次续租，并最终返回真实 chunk。
- Chat/学习组合的新增回归：`test_legacy_rag_stream_renews_receipt_while_waiting_for_first_chunk` 通过；后续全量门禁记录在执行证据中。

## 边界

- 这是本机 Chat 收据租约的存活观测，不是模型调用可取消、客户端断线自动恢复或外部 exactly-once。
- 后台 worker 仍可能在客户端断开后完成；因此不会把断线直接标记为可安全重试，重试仍由收据状态和租约决定。
- 零宽 heartbeat 是传输层保活信号，不是回答内容，也不替代真实网络断线、Tauri 流恢复和模型服务健康检查。
