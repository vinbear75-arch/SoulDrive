# 邮件提交后审计故障边界优化

日期：2026-09-13

## 对照实践

- [AWS Transactional Outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html) 将“主数据已提交、通知失败”定义为双写一致性风险，并要求消费者具备幂等性。
- [Open WebUI audit implementation](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/audit.py) 将审计作为独立的观测路径，而不是让审计异常覆盖业务结果。

## 失败优先发现

本地反例在旧实现上确认了三个问题：

1. 草稿状态已写成 `confirmed` 后，`artifact.status_changed` 审计异常会把接口变成 500。
2. SMTP 授权码已完成核验并保存后，`email.smtp_configured` 审计异常会把成功配置误报成失败。
3. SMTP 已返回成功且 `email_deliveries.status=sent` 已提交后，`email.sent` 审计异常会落入发送异常路径，把状态再次改成 `uncertain`，并可能诱导用户重试。

## 本轮修改

- 草稿确认、SMTP 配置和邮件发送成功路径统一使用 `_append_audit_best_effort`。
- 审计失败不再改变已提交的主状态；响应保留业务成功结果并返回 `warning`。
- 邮件发送失败/不确定路径的失败审计也改为 best-effort，原始投递状态和错误优先返回。
- UI 在草稿确认、SMTP 配置和邮件发送成功提示中展示后置审计告警。

## 验收证据

- 失败优先新增 3 个反例全部通过：
  - `test_draft_status_stays_successful_when_audit_fails`
  - `test_smtp_configuration_stays_successful_when_audit_fails`
  - `test_email_send_stays_sent_when_success_audit_fails`
- 邮件/草稿相关回归：`34 passed, 1 warning`。
- 前端邮件契约：`1 passed`；TypeScript/Vite production build 通过。
- 完整 `scripts/check.ps1`：后端 `1296 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是事务性审计 outbox，也不保证审计事件自动补偿、跨进程 exactly-once 或外部 SMTP exactly-once。
- SMTP 外部投递成功后的状态仍以本地 durable delivery receipt 为准；网络断线导致 provider 结果未知时继续保持 `uncertain`，不会因审计告警改变该结论。
- 完整工程门不等于真实 Tauri/UI 点击、真实 SMTP 投递或 Windows 凭据管理器 E2E。
