# 记忆同步有界重试与人工恢复优化

日期：2026-09-14

## 发现

记忆主记录提交后，关键词/向量/文件归档等派生投影通过本地
`learning_sync_outbox` 异步重放。原链路虽然已有租约、错误指纹和指数退避，
但失败事件可以无限保持 `pending`：长期故障时会持续占据恢复队列，健康状态也
无法区分“稍后自动重试”和“需要人工处理”。这与 durable execution 的最大尝试
次数、暂停和人工恢复语义不一致。

## 公开实践对照

- [Restate retry configuration](https://docs.restate.dev/services/configuration) 和
  [Restate error handling](https://docs.restate.dev/guides/error-handling) 将最大尝试次数、退避、暂停/终止和人工恢复作为显式运行状态；本地只借鉴状态机，不引入服务编排运行时。
- [Dapr pub/sub](https://docs.dapr.io/developing-applications/building-blocks/pubsub/pubsub-overview/)
  与 [Dapr dead-letter topics](https://docs.dapr.io/developing-applications/building-blocks/pubsub/pubsub-deadletter/)
  强调失败消息不能无限阻塞正常消费，且应保留可诊断、可恢复的失败路径。
- [Dapr transactional outbox](https://v1-12.docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/)
  保留至少一次投影与幂等处理的边界；本地不把暂停恢复包装成 exactly-once。

## 实际改动

1. 为已有 `learning_sync_outbox` 增加兼容迁移字段 `retry_paused_at`，不改变原表的
   `pending/completed` 持久化状态；对外列表在该字段存在时派生为 `paused`，旧数据库
   可自动补列。
2. 失败尝试上限固定为 `MEMORY_SYNC_MAX_ATTEMPTS = 5`。达到上限后清空下一次尝试
   时间和租约，设置暂停时间；领取器排除暂停事件，并在发现旧版本遗留的过限事件时先
   补成暂停态。
3. `memory_health()` 增加 `paused_syncs` 计数和不含正文的
   `paused_memory_sync` 错误项；普通退避仍作为待处理警告，暂停事件升级为需要处理的
   健康错误。
4. 增加 `POST /learning/memory-sync/{event_id}/resume?project_id=...`。恢复会校验
   解密后的项目范围：项目事件必须提供完全匹配的 `project_id`，全局事件不接受项目
   参数；恢复时重置尝试计数和租约，并写入 `learning.memory_sync_resumed` 审计事件。
5. 恢复后仍使用原有租约、错误指纹、指数退避和至少一次投影，不对外部文件、向量或
   模型调用宣称事务性 exactly-once。

## 失败优先与验收

- 新增学习存储回归：连续 5 次领取/失败后事件变为 `paused`，不再被领取；健康检查
  报告 `paused_syncs=1` 和 `paused_memory_sync`；人工恢复后尝试数归零，重新领取并完成。
- 新增 API 回归：项目 `project-a` 的暂停事件用 `project-b` 恢复返回 `409`，用
  `project-a` 返回 `200/resumed`。
- 学习存储与学习 API 组合：`182 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1383 passed, 1 warning`；前端生产构建通过，8
  组 UI 测试合计 `61 passed`；`compileall` 与 `cargo check --locked` 通过。
- `git diff --check` 无补丁空白错误；其中已有工作区文件的 LF/CRLF 提示属于 Git
  换行符警告，不是本轮代码错误。

## 边界

这是本机单用户、单工作区 SQLite outbox 的有界恢复机制，不是跨数据库事务、消息代理
dead-letter 集群、外部投影 exactly-once 或自动判断业务故障原因。暂停后的事件需要用户
或运维确认外部依赖已恢复后调用恢复入口；未调用恢复时，系统应保持可见的暂停错误，
而不是偷偷无限重试。真实 Tauri/WPS UI、外部向量服务和 SMTP 投递仍不在
`scripts/check.ps1` 的证明范围内。
