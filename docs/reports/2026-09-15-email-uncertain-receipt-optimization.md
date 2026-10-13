# 邮件外部副作用与未知回执优化

日期：2026-09-15  
范围：已确认邮件草稿发送、SMTP 外部副作用、`email_deliveries` 幂等回执。

## 外部实践与当前缺口

本轮对照 [Temporal retry policy](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx)、[Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests) 与 [transactional-outbox](https://github.com/tomorrow-one/transactional-outbox)：外部调用跨过发送边界后，连接断开或本地结果保存失败不能被解释为“安全失败”；需要保存稳定幂等身份、复用已知结果或明确进入人工核验的未知态。普通 SMTP 本身没有服务端幂等键，SoulDrive 不能宣称 exactly-once。

旧链路在 SMTP 抛出 `EmailDeliveryError` 后，如果本地 `finish_email_delivery(..., 'uncertain', ...)` 再次失败，异常会从异常处理分支逸出为泛化 500；记录若停在 `sending`，相同幂等键的后续请求也只能得到笼统“已尝试”冲突。

## 最小改动

- 已跨过发送边界的异常分支，持久化未知态失败时仍返回 `delivery_status=uncertain`，不把本地回执故障覆盖成普通服务器错误。
- `reserve_email_delivery()` 对同一幂等键的 `sending` 记录返回 `uncertain`；发送结果未被本地确认时，后续请求只能核验或创建新的已确认业务流程，不能自动重发。
- 既有 `sent` 回放、参数摘要冲突、发送前失败可重试和 SMTP 结果未知回归保持不变。

## 回归证据

- 失败优先：SMTP 已进入发送边界且异常回执写入失败时，接口仍返回 502/uncertain；随后同键查询仍返回 uncertain。
- `tests/test_topic5_drafts.py tests/test_project_catalog.py`：`60 passed, 1 warning`。
- 完整工程门禁：后端 `1582 passed, 1 warning`（约 364 秒），前端生产构建成功、UI 脚本 `62 passed`，Python `compileall` 与锁定 Cargo 通过。

## 边界

- 本轮只保证应用层不把未知发送结果误报为可安全重试；SMTP 服务端不支持幂等查询时，不能判断消息是否已到达。
- 未引入重量级邮件队列、CDC 或云邮件连接器；真实 SMTP/provider 行为、跨进程故障窗口和重复投递概率仍未实测。
