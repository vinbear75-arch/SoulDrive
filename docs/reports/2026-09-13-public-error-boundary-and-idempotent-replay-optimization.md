# 2026-09-13 公共错误边界与幂等失败回放优化

## 目标

本轮针对本机单用户 API 的公开投影做失败优先审计：内部异常可以进入本地日志用于诊断，但 HTTP/SSE、审计 payload 和幂等失败重放不应直接返回异常原文、私有路径、连接串或 token。重点覆盖 Skill 管理、聊天启动、文档删除和索引任务幂等收据。

## 外部实践对齐

- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai-spans.md)：错误以结构化属性和事件关联，不把整段异常文本当作公开业务结果。
- [Langfuse observation types](https://langfuse.com/docs/observability/features/observation-types)：区分 observation 状态、错误和 trace 关联，保留可聚合的诊断维度。
- [Restate durable execution](https://docs.restate.dev/foundations/key-concepts)：持久执行的业务结果与请求生命周期分离，重试/重放不能把一次失败误报为新的外部副作用。
- [Temporal activity execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx)：活动可能重试，失败回调和公开投影需要可关联但不应依赖异常原文。

## 实际改动

1. Skill 列表、同步、PPT 样式适配和删除的后端异常统一返回固定用户提示、`stage` 与 `error_type/error_chars/error_digest`；删除后的列表刷新 warning 固定化，并新增 `warning_observation`。不再返回 `detail` 原文或数据库失败分支中的本地 `path`。
2. `/chat` 工作区启动解析失败保留 SSE 合同，改为固定提示；本地审计记录 `chat.workspace_unavailable` 的有界指纹，不把工作区异常文本写入回答流。
3. 文档删除在索引清理失败时返回固定错误与错误指纹，保持源/加密文档记录未删除的业务语义。
4. `/index/run` 对已失败幂等收据重放时返回固定失败提示和 `error_observation`，不再把收据中的失败原文回放给客户端；同一 `Idempotency-Key` 的成功/失败状态仍保持可识别。

## 验收证据

- 受影响模块组合：`142 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1366 passed, 1 warning`；前端生产构建通过，8 组 UI 测试合计 `61 passed`；`compileall` 和 `cargo check --locked` 通过。
- 失败优先反例覆盖：Skill 后端异常含私有 token、聊天工作区异常含本地路径和 token、文档索引清理异常含 Redis 连接串、索引失败收据含后端 token；对应公开响应均不包含原文。

## 边界与未完成项

本轮只关闭已复现的公开投影边界，不宣称所有本地日志、所有参数校验错误、所有 durable payload 已统一脱敏；也不等于已接入 OpenTelemetry/Langfuse 后端、跨进程 exactly-once、真实网络断线恢复或 Tauri/WPS E2E。下一轮继续审计运行状态中的索引失败 reason、项目删除失败持久化和其他外部副作用回放路径。
