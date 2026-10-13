# SoulDrive 邮件外部副作用结果未知状态优化

日期：2026-09-10

本轮针对 durable execution 与幂等键继续做外部实践对照，修复一个由当前代码可复现的状态机缺口：邮件发送预留后，外部提供方抛出未分类异常时，系统不能把“发送前失败”和“调用已开始但结果未知”混为一谈。

## 外部实践对照

- [Temporal 工作流定义](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/workflow/workflow-definition.mdx)明确把外部活动放在可重试的 Activity 边界，并要求活动具备幂等性或明确不可重试；[Temporal 架构说明](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md)也把外部活动的至少一次/不可重试边界与工作流确定性分开。
- [Inngest 幂等性](https://www.inngest.com/docs/guides/handling-idempotency)使用输入绑定的幂等键折叠重复触发；[Durable Execution](https://www.inngest.com/docs/learn/how-functions-are-executed)要求非确定性数据库/API调用位于可持久化 step 内。
- 这些机制不能证明 SMTP 提供方在网络断开后到底是否已接受邮件，因此本地系统不能把“调用异常”自动当成安全失败并放开重试。

## 发现的缺口

`/drafts/{artifact_id}/send` 先写入 `email_deliveries.status = sending`，再读取附件并调用外部发送。原逻辑只捕获已知的配置/业务错误；未分类异常会直接离开路由，`sending` 记录既没有清晰的结果语义，也没有告诉调用方是否可以安全重试。若异常发生在提供方已接受消息之后，自动改成 `failed` 会制造重复邮件风险。

## 最小实现

- 在实际调用 `send_confirmed_email` 前设置 `delivery_attempt_started`；本地准备阶段异常仍写入 `failed`，允许使用新幂等键重试。
- 外部发送调用开始后，任何未分类异常（包括发送后本地完成标记失败）写入 `uncertain`，记录错误和审计事件 `email.uncertain`。
- 同一幂等键重放 `uncertain` 记录返回 `409`，明确要求先核实提供方；不自动换键重发。
- 已成功的发送仍返回幂等成功；已知附件/配置错误仍保持原来的可重试失败语义。

## 验证证据

- `tests/test_project_catalog.py tests/test_topic5_drafts.py`：`35 passed, 1 warning`。
- 项目/API/邮件/PPT 组合：`63 passed, 1 warning`。
- `scripts/check.ps1`：后端 `1085 passed, 1 warning`；Python compileall 通过；前端构建通过、7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。
- 故障注入覆盖：提供方异常 → `uncertain` 且同键重放被阻断；本地附件存储异常 → `failed` 且新幂等键可重新预留。

## 明确边界

- `uncertain` 是保守的人工/提供方核对门，不是 SMTP exactly-once 证明，也没有实现提供方状态查询或撤销发送。
- 真实邮件仍需用户单独确认；测试只使用 mock/provider sink，不触发真实外部发送。
- 后续若接入支持消息查询的提供方，应在单独的 provider adapter 中增加“按 Message-ID 查询”而不是放宽本地重试门。
