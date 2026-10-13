# 会话与联系人删除后置审计故障边界优化

日期：2026-09-13

## 发现

联系人删除和项目会话删除都先提交主数据删除，再追加 `contact.deleted`/`session.deleted` 审计。旧实现中审计 sink 异常会让已完成删除返回 500；联系人 UI 显示失败，会话 UI 也不消费任何 warning。

## 公开实践对照

- [AWS Transactional Outbox Pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)：业务状态与事件通知是两个写入边界，不能用一次 HTTP 返回假装二者已经原子完成。
- [Microsoft Transactional Outbox](https://learn.microsoft.com/en-us/azure/architecture/databases/guide/transactional-out-box-cosmos)：事件意图应可持久化、可重试；在尚未建设 outbox 时，至少要把主状态和通知失败如实分开。
- [Restate Durable Execution](https://docs.restate.dev/foundations/key-concepts)：已完成副作用在恢复时不能重新执行，后续通知失败不应回滚或伪装覆盖前置结果。

本轮没有新增 outbox 表或消息代理，只统一采用当前本机模式下的 best-effort audit + warning 边界。

## 实现

1. 会话删除与联系人删除在主数据提交后调用 `_append_audit_best_effort()`。
2. 主删除成功始终返回 `status=deleted`；审计失败附带 bounded `warning`，不返回伪造的 500。
3. UI 保留本地列表更新，同时显示审计 warning；实际删除/权限/范围错误仍按原错误码处理。

## 验收

- 会话/联系人失败优先定向回归：`2 passed, 1 warning`。
- 文档范围 UI 契约：`23 passed`；生产构建通过。
- 完整 `scripts/check.ps1`：后端 `1293 passed, 1 warning`；前端构建及 8 组测试共 `59 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这证明的是本机主数据删除与后置审计失败的结果分离，不证明联系人/会话删除响应丢失后的专用回放 receipt。
- 审计仍不是事务性 outbox；warning 不能证明事件已经落盘，也没有自动补偿。
- 不改变删除权限、项目范围校验、主对话恢复或联系人邮箱合并规则。
