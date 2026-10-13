# SoulDrive 请求回执与并发租约优化

日期：2026-09-09

本轮继续采用“外部实践对照 → 当前代码缺口 → 先写失败测试 → 最小实现 → 定向回归”的闭环。目标是修补聊天幂等键已经存在、但首条用户消息尚未落盘时仍可能重复进入模型的竞态。

## 外部实践与取舍

- [Temporal 架构说明](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md)把工作流状态放在可重放的事件历史中，并要求可重试 Activity 具备幂等性。
- [Temporal 重试策略](https://github.com/temporalio/documentation/blob/main/docs/encyclopedia/retry-policies.mdx)明确区分工作流重放与 Activity 重试：失败操作应在具体步骤边界重试，不应无条件重复整个流程。
- [Restate 项目](https://github.com/restatedev/restate)将可靠执行、持久化步骤和幂等调用作为基础原语；其[安全入口重试说明](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.4.md)强调非幂等请求必须带幂等键，避免入口重试造成重复执行。

SoulDrive 当前是单机 Windows 单用户应用，不适合为一个首请求竞态引入 Temporal/Restate 服务。可迁移的最小机制是：同一 `session_id + request_id` 先写入本机持久回执；未过期时拒绝第二个执行；进程崩溃留下的过期回执允许接管；完成结果由现有加密会话归档重放。

## 本轮实现

### 1. 本机 request receipt 状态机

`core/conversation_store.py` 新增加密会话 SQLite 的 `request_receipts` 表。表中只保存问题摘要，不保存原问题文本：

`不存在 → in_flight(带 owner/lease_token/过期时间) → completed | failed`

- `claim_request` 使用 SQLite `BEGIN IMMEDIATE`，使不同进程/不同连接只有一个请求获得 `claimed`。
- 同一键绑定不同问题摘要返回 `conflict`。
- 活跃租约返回 `in_flight` 和剩余重试时间，不进入模型加载。
- 过期租约可由新 worker 接管；失败回执可重新领取。
- `complete_request` 和 `fail_request` 必须匹配 owner 与 lease token，旧 worker 不能凭旧 token 改写新的执行状态。
- 流式聊天利用已有 SSE 心跳按需调用 `renew_request`；长时间没有新 token 时不会因为固定租约到期而被误接管。回执只保存摘要、状态和时间，不把问题原文或异常文本写入本机索引。

### 2. 聊天入口接入

`core/mcp_server.py:/chat` 在所有会话、项目、实验上下文校验完成后、模型加载前领取回执：

- 已完成且存在 assistant 归档时仍直接重放。
- 已有有效执行时返回 `409`，带 `request_id`、`retry_after_ms` 和 `Retry-After`。
- 工作区锁定、索引繁忙或生成失败会释放为可重试的 `failed` 回执。
- assistant 消息持久化后才标记 `completed`，并记录响应消息 ID。

`core/state_backends.py` 仅做代理；回执事实源仍是本机加密会话数据库，符合当前单机边界。

## 验收证据

- `tests/test_conversation_store.py`：同一句柄的进行中/完成/冲突、过期接管、失败后重试。
- `tests/test_conversation_store.py`：两个 SQLite 连接并发领取同一键，结果严格为一个 `claimed`、一个 `in_flight`。
- `tests/test_mcp_server_learning.py`：聊天入口在已有进行中回执时返回 `409`，且不加载模型；已有完成回答仍可重放。
- 定向回归：先通过 `59 passed, 1 warning`，加入续租和评测契约后的组合回归为 `96 passed, 1 warning`。
- 首次全量回归暴露了轻量会话替身返回 `None` 时读取 `message_id` 的兼容性回归；修复后执行 `scripts/check.ps1`：后端 `998 passed, 1 warning`。完成态修正新增回归后为 `999 passed, 1 warning`；后续记忆时间有效性、答案来源可追溯性、TaskJournal fencing、聊天历史时点路由、记忆版本历史、工作流文档范围、中断 fencing、邮件预留竞态、长步骤恢复保护、记忆审核并发、自动记忆写入边界、审核草稿项目绑定和自动记忆候选范围循环再次复跑：后端 `1019 passed, 1 warning`，前端生产构建通过，7 组前端测试共 `44 passed`，Tauri `cargo check --locked` 通过。

## 明确边界

- 这是本机 SQLite 的 at-least-once 执行保护，不是外部模型调用或邮件/日历写入的 exactly-once 证明。
- 如果旧 worker 在租约过期后仍继续运行，新的 worker 可以接管；lease token 会阻止旧 worker 完成回执，但无法撤销已经发生的外部副作用。因此邮件、日历、PPT/WPS 等不可逆操作仍需各自的业务幂等键、结果查询和确认门。
- 本轮没有实现跨主机租约或分布式时钟，也没有改变当前不建设云端协同的产品边界；心跳续租只在当前本机流式请求内生效。
- RAG 留出集的真实模型答案质量、真实 Tauri/WPS 视觉链路和跨主机租约仍需在后续循环中继续验证；本轮全量门通过不等于这些人工/真实运行门已通过。
