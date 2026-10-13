# SoulDrive 邮件投递边界与请求摘要幂等优化

日期：2026-09-12

## 本轮结论

本轮继续对照 durable execution 与外部副作用幂等实践，修复了邮件发送链路的两个可复现缺口：

1. `EmailDeliveryError` 可能来自 SMTP 调用之后的网络断开或响应丢失，旧逻辑会把它记为 `failed`，从而放开重试；现在跨过外部调用边界后统一记为 `uncertain`，同键重放要求先核验提供方。
2. 发送记录原来只绑定草稿和幂等键，没有绑定发送请求参数；同一键改用不同发件人时，旧逻辑会静默返回成功。现在把规范化发送请求摘要保存在已有 `email_deliveries.detail_json` 中，同键不同摘要返回冲突。

## 外部实践对照

- [Stripe Idempotent requests](https://docs.stripe.com/api/idempotent_requests) 将幂等结果与键及原始请求参数绑定；同键参数不一致时拒绝复用，执行尚未开始的校验失败不应污染幂等结果。
- [AWS Transactional outbox pattern](https://docs.aws.amazon.com/en_en/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) 明确指出外部通知可能重复，发送方/消费方必须通过持久化状态和幂等处理控制重复；它不能把外部系统的 exactly-once 传递给本地 SMTP。
- 这些实践映射到 SoulDrive 的本机单用户场景后，最小实现应保持现有 SQLite 状态表和人工核验门，不引入队列或工作流框架。

## 失败优先证据

- 新增 `test_smtp_delivery_error_after_send_boundary_is_uncertain_and_not_retryable`：先用 `EmailDeliveryError` 复现旧逻辑返回普通 `failed`、缺少 `delivery_status` 的问题；修复后返回 `uncertain`，同键重放为 `409`。
- 新增 `test_email_send_rejects_same_key_with_different_sender_parameters`：先复现同一已发送草稿用发件人 A/B 重放都返回 `200` 的问题；修复后第二次返回 `409`，发送适配器只调用一次。

## 最小实现

- `/drafts/{artifact_id}/send` 以 `delivery_attempt_started` 区分本地准备失败与 SMTP 外部调用后的 `EmailDeliveryError`。
- `ProjectCatalog.reserve_email_delivery()` 增加可选 `request_digest`，在现有 `detail_json` 中保存摘要；同键同摘要仍返回原状态，同键不同摘要返回 `conflict`。
- `finish_email_delivery()` 合并状态详情而非覆盖预留摘要，保证 `sent`、`failed` 和 `uncertain` 的审计信息都能保留幂等绑定依据。
- 摘要当前只绑定规范化发件人；草稿内容、收件人和附件已经由不可变/已确认 artifact 绑定。若未来允许确认后修改邮件计划，必须把计划摘要纳入同一 request digest 并使原确认失效。

## 验证证据

- 邮件与项目目录专项：`40 passed, 1 warning`。
- 仓库级 `scripts/check.ps1`：后端 `1171 passed, 1 warning`；Python compileall 通过；前端生产构建通过，7 组前端测试 `49 passed`；Tauri `cargo check --locked` 通过。
- Vite 仍报告既有的大 chunk 警告；这不是本轮功能失败，也未据此宣称性能优化完成。

## 明确边界

- `uncertain` 不是 SMTP exactly-once 证明；本机仍无法仅凭超时判断提供方是否已经接收邮件。
- 真实 SMTP 发送、提供方 Message-ID 查询和撤销仍需用户/提供方确认；自动测试只使用 mock。
- 当前摘要绑定范围是发送请求的发件人，因为已确认草稿及其附件绑定提供其余输入身份；扩大邮件计划可编辑能力时必须重新评估。
