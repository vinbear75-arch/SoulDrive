# 邮件发送前失败的错误边界优化

日期：2026-09-13

## 发现

邮件发送接口已经区分了发送边界后的 `uncertain`：SMTP 调用开始后不能把结果当作可安全重试。但发送前的本地附件/配置预检异常仍可能把异常原文直接放入 HTTP 响应、审计事件和 delivery detail。异常原文可能包含私有路径、连接串或凭据片段，且发送前失败本来是可重试的确定失败，不需要泄露这些内部细节。

## 公开实践对照

- [Restate request lifecycle](https://docs.restate.dev/guides/request-lifecycle) 将 durable 状态与调用边界分开，失败状态应能被恢复，而不是把内部异常文本当作客户端协议。
- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai-spans.md) 将错误类型、状态和关联信息作为观测维度；本机实现只保留有界错误类型、长度和摘要，不把异常正文放进公开投影。
- [sqlite-durable-workflow](https://github.com/eatdrop/sqlite-durable-workflow) 明确区分可重试失败与 `delivery_unknown`；本轮沿用项目已有的发送前 `failed` 与发送后 `uncertain` 两态，不把两者混为一谈。

## 实现

1. 发送前配置/预检异常统一返回固定的“邮件发送失败，可检查后重试”提示。
2. delivery detail 和 `email.failed` 审计只保留 `error_observation`：异常类型、字符数和短摘要；响应也只返回同一有界观测。
3. SMTP 调用边界后的 `uncertain` 仍保留不可安全自动重试的既有语义，并继续只保存错误指纹。
4. 增加包含私有路径、SMTP token 和连接串的发送前异常反例，检查响应和持久化 detail 均不含秘密文本。

## 验收

- 发送前异常定向测试先验证了旧响应会泄露原文，再在修复后通过；邮件与连接器组合回归 `38 passed, 1 warning`。
- 仓库级 `scripts/check.ps1` 随本轮 SQLite 幂等改动通过：后端 `1376 passed, 1 warning`；前端构建与 UI 测试 `61 passed`；`compileall` 与 `cargo check --locked` 通过。

## 边界

- 这只覆盖本机接口、审计和 delivery detail 的错误投影；不证明真实 SMTP 投递成功率、提供方查询或外部 exactly-once。
- 错误摘要用于本地关联，不是把原始异常恢复给用户的诊断接口；需要详细诊断时仍应在受控本机日志中按安全策略查找。
