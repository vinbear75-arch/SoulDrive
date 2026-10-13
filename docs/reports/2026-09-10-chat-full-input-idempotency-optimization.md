# 聊天回执缺失时的完整输入幂等门优化记录

日期：2026-09-10

## 结论

本轮修复了聊天请求的一条可复现幂等缺口：历史助手消息仍在、但 `request_receipts` 回执缺失时，旧逻辑会按 `request_id + query` 先行重放，无法证明选区、工作流、项目和其他显式上下文仍然相同。现在聊天只有在持久回执已经绑定当前完整输入，或历史助手消息自身保存了相同的 `input_digest` 时才允许重放；旧的 query-only 历史消息会 fail-closed，返回 `409` 并要求新的 `request_id`。

## 外部实践对照

- [Stripe 幂等请求](https://docs.stripe.com/api/idempotent_requests?javascript=false&lang=node)把同一幂等键与请求参数绑定；同键参数不一致时拒绝，而不是把第一次结果静默套到第二次输入上。SoulDrive 继续采用本地 SQLite 回执，不引入 Stripe 依赖，只迁移“键与规范化完整输入绑定”的边界。
- [Prefect caching](https://docs.prefect.io/v3/concepts/caching)把任务输入、代码定义和运行范围纳入 cache key，避免仅按任务名称命中。SoulDrive 的聊天输入摘要现在覆盖 query、top-k、项目、会话角色、时间、显式历史、感知事件和 active context，并在助手归档中保留摘要而不保存明文上下文副本。
- [Temporal durable execution architecture](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md)要求工作流保持确定性，外部 Activity 必须幂等或不可重试；本轮将“历史回答回放”视作一个必须先验证输入绑定的可重试步骤，没有把本地模型调用宣称为 exactly-once。

## 实现

- `ProductionSessionStore` 增加按精确 `message_id` 读取助手归档的薄封装；已完成回执优先使用其保存的响应消息 ID，旧回执才退回同 `request_id` 的已完成助手查询。
- `/chat` 不再在取得持久回执前把 query-only 助手消息当作可信重放结果；这避免回执缺失时绕过完整输入摘要门。
- 新写入的用户/助手消息都保存 `input_digest` 元数据，正文仍保持现有本地加密归档边界。
- 如果回执过期或丢失但助手消息带有相同摘要，则可在新租约下恢复重放；若摘要缺失或不一致，释放新租约并返回 `409`，不加载模型、不重新生成、不覆盖历史回答。

## 失败优先验证

- 新增 `test_chat_does_not_replay_legacy_archive_without_full_input_binding`：构造没有回执、只有旧 query-only 助手归档且选区已变化的请求，确认返回 `409`，不会进入模型加载。
- 原有已完成重放测试已改为带完整摘要的归档，确认无回执但摘要一致时仍能安全重放。
- 幂等/会话/学习同步/TaskJournal 组合：`171 passed, 1 warning`。
- 本轮最新 `scripts/check.ps1`：后端 `1120 passed, 1 warning`；前端生产构建成功，7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机单用户 SQLite 回执与加密会话归档内的 at-least-once 边界，不是跨模型、文件系统或外部邮件/日历系统的 exactly-once。
- 对缺少完整摘要的旧历史消息选择拒绝重放，可能要求用户换一个幂等键重新提交；这是迁移期间的保守行为，避免把上下文不明的旧答案送回当前窗口。
- 本轮没有引入 Temporal、Prefect 或分布式回执服务，也没有扩大真实 Tauri/WPS/E2E 验收范围。
