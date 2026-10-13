# 2026-09-13 邮件 uncertain 错误指纹优化

## 结论

跨 SMTP 调用边界后，发送结果可能已经被供应商接受但响应丢失；SoulDrive 已将这类状态标记为 `uncertain` 并禁止盲目重试。本轮补齐观测边界：供应商异常原文不再写入 `email.uncertain` 审计或本地 `email_deliveries.detail_json`，改为异常类型、长度和 16 位 SHA-256 指纹。发送前的本地校验失败仍保留原有可行动错误和 `failed` 状态。

## 外部实践对照

- [Temporal activity execution](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/activities/activity-execution.mdx) 将外部活动取消、心跳和执行状态分开处理；外部调用边界后的结果不能被本地异常文本简单解释为“未发送”。
- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md) 采用结构化错误类型与状态属性关联失败，而不是把完整输入/输出正文作为必需观测字段。
- 本项目已有邮件 durable execution 约束：`uncertain` 需先向提供方核验，不能用同一幂等键直接再次发送；本轮只收紧错误观测，不改变该状态机。

## 失败优先反例

扩展 `tests/test_topic5_drafts.py::test_smtp_delivery_error_after_send_boundary_is_uncertain_and_not_retryable`：模拟 SMTP 已跨调用边界后抛出包含 `secret=SMTP-TOKEN` 和私人收件人的异常，并检查：

- HTTP 返回仍为通用 `uncertain`，不回显异常正文；
- `email.uncertain` 审计序列化结果不含异常正文；
- `email_deliveries.detail_json` 不含异常正文；
- 相同幂等键继续返回 `409 uncertain`。

该反例通过 `1 passed`；Chat 错误指纹回归也通过 `1 passed`。

## 实现范围

- `core/mcp_server.py` 抽出通用 `_error_observation()`。
- 仅当 `delivery_status == uncertain` 时，delivery detail 和审计使用指纹；本地发送前失败仍保留原始错误，便于定位附件绑定、配置和本地存储问题。
- 没有改变 SMTP 发送、幂等键、提供方核验或重试策略。

## 验证证据

- SMTP uncertain 失败优先回归：`1 passed, 1 warning`。
- 修改后的完整 `scripts/check.ps1`：后端 `1355 passed, 1 warning`；前端构建及 8 组 UI 测试 `61 passed`；`compileall` 与 `cargo check --locked` 通过。
- Vite 仍提示既有单个 JS chunk 超过 500 kB；这不是本轮邮件逻辑失败。

## 边界

- 该指纹不能恢复供应商真实投递结果；`uncertain` 仍需人工/提供方核验。
- 本轮不声称 SMTP exactly-once、真实邮箱投递、凭据轮换、远程 trace 后端或真实 Tauri/WPS UI/E2E 已完成。
