# PPT 完成态回放与 worker 边界优化

日期：2026-09-13

## 发现

PPT 计划已经按稳定 `request_id` 生成持久化任务。网络超时后再次提交同一请求时，任务日志会返回同一任务，但旧实现仍会让 `run_task()` 对已完成任务走一次收尾，并且 HTTP 入口仍尝试再次拉起 worker，造成重复完成审计与无意义线程生命周期。

## 公开实践对照

- [Trigger.dev durable task idempotency](https://github.com/triggerdotdev/trigger.dev/blob/main/rules/4.0.0/advanced-tasks.md) 将幂等键绑定到 task run，并要求重试沿用同一 key；本地把已持久化的 task status 作为 replay 的第一判断。
- [Inngest SDK specification](https://github.com/inngest/inngest/blob/main/docs/SDK_SPEC.md) 将 event ID 作为一次事件的稳定处理身份，并把完成步骤结果记忆化；本地不引入队列，只让完成态返回已记录结果。
- [torkbot/sledge](https://github.com/torkbot/sledge) 明确指出 durable work 可 at-least-once，外部效果必须使用稳定 key；本轮只消除本地完成态的重复收尾，不宣称 PPT/WPS exactly-once。

## 实现

1. `PresentationService.run_task()` 读取任务后，如果状态已经是 `completed`，直接返回持久化任务，不再重新调用 `run_steps()`、同步 artifact 或写完成审计。
2. `POST /presentations/generate` 读取公开任务后，`completed/cancelled` 状态不再启动 presentation worker；pending/running/failed/interrupted 仍走既有恢复路径。
3. 计划仍由 `request_id` 和完整输入摘要绑定；改动只针对完成态回放，不改变失败任务重试、用户确认门或取消语义。

## 失败优先与验收

- 旧实现反例：已完成任务再次 `run_task()` 会多写 `presentation.task_completed`；已完成计划通过 HTTP 重试仍调用 `_start_presentation_thread()`。
- 修复后 PPT 服务/API 定向 `30 passed, 1 warning`，覆盖完成态回放审计计数和 HTTP worker 未启动。
- 完整 `scripts/check.ps1`：后端 `1273 passed, 1 warning`；前端生产构建与 7 组测试共 `53 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机完成态 replay 的副作用抑制，不是跨进程 lease、外部 WPS 启动或 PPTX 文件写入的 exactly-once 证明。
- 失败/中断任务仍可能按 durable step 语义重试；外部可观察步骤仍依赖已有 step 幂等键与恢复边界。
- 该证据不覆盖真实 Tauri/WPS/UI/E2E、网络断线注入、跨进程线程竞争或生成内容质量。
