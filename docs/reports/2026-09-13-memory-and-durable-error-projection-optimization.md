# 2026-09-13 记忆同步与 durable 收据错误投影优化

## 目标

本轮继续审计本机单用户模式下的 durable receipt、记忆派生同步和运行状态公开投影。目标不是把所有错误变成不可诊断的通用文本，而是让公开响应和可重放记录只保留稳定的状态、错误类型、长度和摘要；内部重试仍保留 attempts、lease、next-attempt-at 和项目范围。

## 外部实践对齐

- [sqlite-durable-workflow](https://github.com/eatdrop/sqlite-durable-workflow)：面向单机 SQLite 的任务/Outbox 实现明确区分 at-least-once、有限退避和 `delivery_unknown`，并把幂等键、租约和可运维状态作为持久记录的一部分。本项目只借鉴边界语义，没有引入该依赖。
- [Dapr Transactional Outbox](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/)：业务状态和消息意图要先持久化，后台投递按 at-least-once 重试；因此记忆 SQLite 事实层、同步 outbox 和公开投影必须分开。
- [OpenTelemetry GenAI spans](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai-spans.md)：失败观测使用结构化属性关联，而不是把异常原文当作业务返回值。
- [Restate durable execution](https://docs.restate.dev/foundations/key-concepts)：重放是持久结果的再次观察，不应把已失败或已完成的状态误报成新的执行。

## 实际改动

1. `/runtime/status` 的索引失败公开投影按 `error_code` 给出固定原因；`source_filename` 只保留安全文件名；原始 reason 只转换为 `error_type/error_chars/error_digest`。
2. `IndexRunJournal` 的 `error` 字段改为结构化错误指纹 JSON；旧版 SQLite 收据在重新打开时迁移为指纹。失败 `Idempotency-Key` 重放仍返回固定文案和原始错误的同一摘要，不回显后端连接串或 token。
3. 项目删除 receipt 的失败字段和 `project.delete_failed` 审计改为固定文案 + 错误指纹；旧版项目删除收据在 catalog 重开时清理原文。项目目录、源目录保留和后续重试语义不变。
4. 记忆归档响应不再返回本机绝对归档路径，只返回 `archive_path: "stored"`；归档失败返回固定文案和错误指纹，嵌套 storage health 复用已有路径/后端错误脱敏。
5. 记忆同步 outbox 的 `last_error` 只保存结构化错误指纹；异常类型保留在 `_replay_learning_memory_outbox` 的失败路径；旧版 outbox 原文在 `LearningStore` 重开时迁移。attempts、lease fencing、退避和项目范围没有改变。

## 验收证据

- 索引幂等收据：`9 passed, 1 warning`；覆盖失败重放、旧版原文收据迁移和同键输入绑定。
- 项目删除与 catalog：定向失败/恢复/保留项目 ID/旧版收据测试通过；失败反例中的 Redis 连接串、路径和 token 未进入 catalog 错误或审计 payload。
- 记忆存储：`74 passed, 1 warning`；覆盖旧版 outbox 错误迁移、lease fencing、退避、重放和时间/范围相关回归。
- 学习接口：`104 passed, 1 warning`；覆盖记忆归档失败公开投影、路径脱敏、运行时记忆幂等及 outbox 回放。
- 完整 `scripts/check.ps1`：后端 `1371 passed, 1 warning`；前端生产构建通过，8 组 UI 测试合计 `61 passed`；`compileall` 和 `cargo check --locked` 通过。

## 边界与下一步

本轮只关闭已复现的公开投影和 durable 失败收据原文边界；不宣称所有参数校验错误、应用日志、所有内部 payload 都统一脱敏，也不宣称跨进程 exactly-once、真实外部服务投递、Tauri/WPS E2E 或 RAG/记忆答案质量已经由这些测试证明。记忆 outbox 仍采用 at-least-once + 有界退避，是否需要人工 dead-letter/最大尝试次数应以真实失败样本和运维入口为依据后再决定。
