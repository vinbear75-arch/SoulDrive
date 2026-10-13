# 本地 CRUD 提交后审计故障边界优化

日期：2026-09-13

## 对照实践

- [Stripe Idempotent requests](https://docs.stripe.com/api/idempotent_requests) 将请求参数与幂等键绑定，并区分首次执行与后续回放。
- [torkbot/sledge](https://github.com/torkbot/sledge) 将 durable event 去重与业务执行结果分开，避免重复事件再次产生副作用。
- [Open WebUI 重复 Knowledge 摄取问题](https://github.com/open-webui/open-webui/issues/24954) 说明内容身份和处理状态需要独立于用户重复操作判断。

## 失败优先发现

旧实现中，项目、项目会话、联系人、邮件草稿、日程草稿创建，以及日程更新/删除在本地事实已经提交后，如果审计 sink 抛错，接口会返回 500。这样客户端会把已完成的本地 mutation 当作失败并重试，增加重复副作用或错误提示风险。

## 本轮修改

- 这些 CRUD 路径统一使用 `_append_audit_best_effort`。
- 主记录提交成功后，响应保持成功状态；审计 sink 故障通过 `warning` 返回。
- 已有 `replayed`/`status` 语义保持不变，真正的权限、唯一键、范围和数据库错误仍按原错误码返回。
- UI 在项目、会话、联系人、邮件/日程草稿的创建、更新和删除提示中保留后置审计告警。

## 验收证据

- 新增失败优先回归覆盖项目创建、联系人创建、项目会话创建、邮件草稿创建、日程草稿创建/更新/删除。
- 项目/草稿/邮件定向回归：`69 passed, 1 warning`。
- 前端完整静态契约：`60 passed`；TypeScript/Vite production build 通过。
- 该轮完整 `scripts/check.ps1`：后端 `1303 passed, 1 warning`；前端构建及 8 组测试 `60 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是事务性 outbox，也不提供跨表、跨文件或跨进程原子提交。
- 审计告警不会自动补偿；当前只保证主业务结果与观测失败不互相伪造。
- 外部邮件、日历、WPS 等副作用不因本地 CRUD warning 获得 exactly-once 证明。
- 完整工程门不等于真实 Tauri/UI 点击、网络断线或多进程压力 E2E。
