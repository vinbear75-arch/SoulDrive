# 邮件发送回放结果可见性优化

日期：2026-09-13

## 发现

邮件发送接口已经用本地持久化幂等键区分首次 SMTP 调用、成功回放、外部结果不确定和可重试失败，但 UI 只检查 `status === "sent"`。因此网络重试命中同一发送记录时，界面仍提示“邮件已发送”，用户无法知道本次没有再次调用 SMTP；在不确定结果场景下，用户也缺少“先核验、不要直接重复发送”的语义提示。

## 公开实践对照

- [Stripe Idempotent requests](https://docs.stripe.com/api/idempotent_requests)：同一幂等键应返回首次请求保存的状态码和响应体；客户端不能把回放当成新的外部副作用。
- [Inngest 错误处理与重试](https://www.inngest.com/docs/guides/error-handling)：重试代码必须保持幂等，外部副作用的重试需要可识别的已有结果。
- [Inngest Steps](https://www.inngest.com/docs/learn/inngest-steps)：非确定性外部调用要在可检查点的步骤中执行，完成后的结果应被复用，而不是盲目重复调用。

本轮只借鉴“结果语义必须穿过客户端边界”的原则，没有引入云端队列或新的发送机制。

## 实现

1. UI 读取发送响应中的 `idempotent` 与 `delivery_status` 字段。
2. `idempotent=true` 且 `status=sent` 时明确显示“发送请求已回放，未重复调用 SMTP”；首次成功仍显示真实发送提示。
3. 保留后端现有 `uncertain/failed` 错误路径；不把不确定结果改成成功，也不在客户端自动换 key 重发。
4. 新增独立前端契约测试，并把它加入 UI 总测试入口；后端回归同时确认首次响应为 `idempotent=false`、重复响应为 `true`。

## 验收

- 邮件草稿/SMTP 定向回归：`29 passed, 1 warning`。
- 新增邮件发送 UI 契约测试：`1 passed`。
- 前端生产构建：通过。
- 完整 `scripts/check.ps1`：后端 `1289 passed, 1 warning`；前端构建及 8 组测试共 `55 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这证明的是本机 sidecar 的邮件发送结果契约和静态/构建级 UI 消费，不是真实 SMTP、真实 Tauri 网络断线或端到端点击证明。
- `idempotent=true` 表示本地发送记录被安全回放，不证明外部邮件服务提供商拥有 exactly-once 投递；外部结果为 `uncertain` 时仍必须人工核验。
- 当前仍没有 SMTP 发送 outbox、供应商级投递回执、自动补偿或跨进程/跨机器 exactly-once。
