# 项目删除完成态与审计故障边界优化

日期：2026-09-13

## 发现

项目删除已经有持久化 deletion receipt 和完成态回放，但删除步骤完成后才写 `project.deleted` 审计。旧代码把审计异常重新纳入删除主流程：项目行、会话、索引和登记的原始目录都已删除，接口却进入失败分支返回 500；失败审计再次抛错时还可能让错误响应本身失真。

## 公开实践对照

- [Restate Sagas](https://docs.restate.dev/guides/sagas)：跨步骤流程要区分已完成的本地操作、补偿和终态错误，不能把后置步骤失败伪装成前置操作未完成。
- [Restate Error Handling](https://docs.restate.dev/guides/error-handling)：外部/派生步骤失败应有明确的终态处理，主流程结果和补偿边界必须可观察。
- [Restate Durable Agents](https://docs.restate.dev/ai/patterns/durable-agents)：步骤状态需要可恢复、可检查，重试不能重复已经提交的步骤。

本轮只借鉴“主状态提交与审计步骤分离”的原则；没有引入分布式 Saga 或新的删除补偿。

## 实现

1. 项目 deletion receipt 完成提交后，`project.deleted` 改用已有 `_append_audit_best_effort()`，审计失败不会重新进入删除失败分支。
2. 成功响应携带 bounded `warning`，明确提示审计记录异常；删除 receipt 保持 `completed`，后续请求仍返回完成态回放。
3. 删除真正失败时，`project.delete_failed` 审计也改为 best-effort，避免错误记录故障覆盖真实业务错误响应。
4. UI 消费 warning，在“项目已删除”提示中保留审计告警，不把已完成操作显示成普通失败。

## 验收

- 项目接口专项：`27 passed, 1 warning`。
- 新增失败优先回归：审计 sink 异常时删除仍返回 `200`，receipt 保持 `completed`，warning 可见。
- 前端文档范围契约：`21 passed`；生产构建通过。
- 完整 `scripts/check.ps1`：后端 `1291 passed, 1 warning`；前端构建及 8 组测试共 `57 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这证明的是本机项目删除主状态与审计故障的结果分离，不证明跨存储原子删除、远程备份恢复或外部 exactly-once。
- 审计 sink 失败只返回 warning，没有新增审计 outbox 或自动补偿；用户仍应按 warning 处理审计缺口。
- 项目删除是不可逆业务操作，UI 仍要求用户确认；本轮没有扩大可删除范围。
