# 2026-09-13 Durable Task 公共错误投影边界优化

## 结论

SoulDrive 的 `TaskJournal` 已经把任务输入、步骤输出和失败原因放入本地加密事实源，但 `public_task()` 之前会把任务、步骤和 attempt history 的 `error` 原文直接返回给 PPT 轮询接口。渲染器、WPS、文件路径或模型后端信息因此可能进入 UI。本轮只收敛公共投影：内部事实源和恢复语义不变，外部改为固定提示与错误指纹。

## 对照实践

- [Restate durable execution](https://docs.restate.dev/foundations/key-concepts) 将可恢复状态和调用结果分开；本轮保留 journal 内部错误用于恢复/排查，不把它当作公共展示字段。
- [Temporal activity execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx) 强调 activity 失败需要可重试、可关联；按任务/步骤/attempt 返回摘要指纹满足本机关联而不复制异常正文。
- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai-spans.md) 使用结构化错误观测；公共任务 projection 采用 `error_type/error_chars/error_digest`。

## 失败优先发现

- `TaskJournal.run_steps()` 将 handler 异常写入加密 SQLite，`public_task()` 又原样返回 task.error、step.error 和 attempt_history.error。
- PPT API 与 UI 轮询 `get_task()`，因此该原文会跨 durable storage 到受保护 HTTP/UI 边界。
- 仅修改 PPT service 的顶层错误还不够；必须在 journal 的公共投影统一处理所有步骤与 attempt。

## 本轮修改

- `public_task()` 的任务/步骤/attempt 错误改为固定中文提示。
- 新增同层级 `error_observation`，包含 `UnknownError`、字符数和 16 位 SHA-256 摘要；内部 `get_task()`、加密存储、重试和 `repetition_guard` 不变。
- 不改变任务状态、lease epoch、检查点、取消/恢复或幂等回放语义。

## 验收证据

- TaskJournal、PresentationService、PPT API 回归：`52 passed, 1 warning`。
- 失败反例包含 `secret failure details`；任务、步骤、attempt history 的 public projection 均不含原文，且保留指纹。
- 修改后的完整 `scripts/check.ps1`：后端 `1362 passed, 1 warning`；前端构建及 8 组 UI 测试 `61 passed`；`compileall` 与 `cargo check --locked` 通过。Vite 仍提示既有单个 JS chunk 超过 500 kB。

## 边界

- 本轮只覆盖 durable task public projection；本机加密 journal 内部仍保留原始失败原因，需继续维持访问边界。
- 指纹不提供错误正文恢复，也不等于接入 Temporal/Restate 服务或跨进程 exactly-once。
- 真实 WPS/Tauri UI、安装包冷启动和外部副作用恢复仍未由该单测门证明。
