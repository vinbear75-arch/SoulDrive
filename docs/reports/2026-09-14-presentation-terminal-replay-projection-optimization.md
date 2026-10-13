# PPT 终态回放与目录投影恢复优化

日期：2026-09-14

## 本轮目标

继续按最终执行大纲 E4/E5 检查 PPT 计划、确认、生成、取消和恢复的 durable 边界。重点不是新增一个任务框架，而是验证同一任务在 HTTP、工具入口和本地重放入口是否遵守同一条规则：终态任务只能回放，不能再次启动副作用；任务事实已提交而目录投影暂时失败时，后续重放必须能够修复投影。

## 公开实现对照

- [Temporal Python SDK：活动取消与 heartbeat](https://github.com/temporalio/sdk-python)：取消是可观察状态，长步骤需要在边界/heartbeat 处感知取消；重试不能把外部副作用简单当作可回滚操作。
- [Trigger.dev durable task idempotency](https://github.com/triggerdotdev/trigger.dev/blob/main/rules/4.0.0/advanced-tasks.md)：同一 task run 的重试沿用稳定幂等身份，已完成结果不应被重复触发成新的执行。
- [Restate durable execution](https://github.com/restatedev/restate)：已完成步骤从 journal 恢复，入口重试应回读持久结果，而不是重新执行已完成步骤。

本项目仍保持本机 SQLite + 加密 TaskJournal + ProjectCatalog 的最小实现，不引入上述运行时。外部实现只用于校验状态、步骤、结果和副作用之间的边界。

## 发现与修复

1. **MCP 工具生成入口绕过终态重放门**

   HTTP `/presentations/generate` 已经不会为 `completed/cancelled` 计划启动 worker，但 `souldrive.presentation_generate` 仍会无条件调用 worker 启动函数。现在工具入口与 HTTP 入口统一：读取公开任务后，终态只返回 `replayed=true/worker_started=false`，未终态才尝试接管 worker。

2. **HTTP/MCP 确认入口对终态生成任务重复启动 worker**

   同一计划重复确认在生成任务已经 `completed` 或 `cancelled` 时，旧入口仍可能启动一个无意义线程。现在两个确认入口都只对非终态任务启动 worker；取消任务仍保持不可恢复语义。

3. **任务事实已完成但 artifact 投影失败时无法自愈**

   `TaskJournal` 先提交计划任务的加密结果，`ProjectCatalog` 再写入 `plan_ready`、大纲摘要和来源映射。如果第二步暂时失败，旧 `run_task()` 对 `completed` 任务直接返回，后续重试永远不会再同步目录。现在终态重放先调用幂等的 `_sync_presentation_artifact()` 修复本地投影，再返回已有任务；不会重新执行证据收集、渲染或 WPS 打开等副作用步骤。

## 验收

- 失败优先回归：新增“已完成计划重放修复缺失 artifact 投影”反例，旧实现确实保持 `not_started`，修复后恢复为 `plan_ready`。
- PPT 服务、HTTP API、MCP 工具入口、TaskJournal 组合：**62 passed, 1 warning**。
- 现有 E4/E5 约束仍保留：实际大纲摘要确认、来源指纹变化阻断、旧未确认生成任务拒绝恢复、步骤间取消、租约/epoch 防迟到 worker 提交、WPS 打开默认不在恢复时重复执行。

## 边界

- 这证明的是本机单 sidecar、SQLite 任务事实与项目目录投影之间的回放边界，不证明 Redis/PostgreSQL 在线部署或跨进程/跨机器 exactly-once。
- 取消仍只保证同步步骤结束后的下一步骤门不再领取；已经开始的 PPT 文件写出或外部 WPS 窗口不能回滚。
- 本轮定向测试不等于真实 Tauri/WPS UI/E2E 或视觉版式验收；完整工程门禁的最终结果单独记录。
