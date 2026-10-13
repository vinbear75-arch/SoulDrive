# 2026-09-13 存储健康状态嵌套错误边界优化

## 结论

外层 `_storage_health()` 的异常已经指纹化，但正常返回的 `ProductionSessionStore.health()` 仍可能在 Redis、PostgreSQL、文件归档、记忆向量重水合和 outbox 子对象中携带异常原文与本地路径。本轮在 sidecar 健康边界统一清理这些字段，保留状态、计数、降级判断和按字段的错误指纹。

## 对照实践

- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai-spans.md) 将错误关联拆成结构化属性；本地健康面采用同样的类型/长度/摘要思路。
- [Langfuse observation types](https://langfuse.com/docs/observability/features/observation-types) 将 observation 状态和错误观测分离；本轮把每个后端字段的错误收敛到 `error_observations`，不把连接串或路径当作状态值。
- [Dapr transactional outbox](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/) 强调 outbox 状态是可观测的持久化边界；SoulDrive 保留 pending 数量和降级状态，但不把 outbox 文件路径暴露给健康端点。

## 失败优先发现

- 外层存储异常测试只能覆盖 `_conversation_store()` 或 `store.health()` 整体抛错，不能覆盖“健康对象内部嵌套错误”的真实返回路径。
- `file_archive` 可能包含 `root`、`sqlite_index_error`、`archive_read_error`；Redis/Postgres 可能包含 `error`/`vector_index_error`；重水合和 outbox 可能携带 `error`/`path`。
- `/health` 是免令牌端点，因此即便本机单用户模式也不能依赖调用者已授权来返回这些原文。

## 本轮修改

- 新增 `_sanitize_storage_health()`，对 file archive、Redis、Postgres、memory rehydrate 和 outbox 逐段清理路径与错误原文。
- 错误按字段写入 `error_observations[field] = {error_type,error_chars,error_digest}`；状态、计数和 `degraded` 计算不变。
- 保护内部异常捕获路径仍返回固定状态错误；不改变存储重连、outbox 重放或降级策略。

## 验收证据

- 存储/健康/限流组合回归：`63 passed, 1 warning`。
- 失败反例包含私有 PostgreSQL 连接串、工作区路径、归档 token 和向量 token；内部健康结果与实际 `/health` 响应均不含这些文本。
- 修改后的完整 `scripts/check.ps1`：后端 `1357 passed, 1 warning`；前端构建及 8 组 UI 测试 `61 passed`；`compileall` 与 `cargo check --locked` 通过。Vite 仍提示既有单个 JS chunk 超过 500 kB。

## 边界

- 该修复只覆盖存储健康响应；其他普通业务验证错误和受控本地日志仍需按暴露边界逐项审计。
- 指纹只用于关联，不表示恢复错误内容，也不等于远程 observability 后端或审计 outbox 已接入。
- 这不证明存储后端高可用、跨存储事务、外部 exactly-once 或真实 Tauri/WPS UI/E2E。
