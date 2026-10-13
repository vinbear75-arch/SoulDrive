# 运行时记忆跨存储重试幂等优化

日期：2026-09-13

## 发现

运行时自动记忆的请求回执保存在 `ConversationStore`，审核草稿、接受态记忆和同步 outbox 保存在 `LearningStore`。原链路已经用会话回执阻止同一 `request_id` 的并发重复进入，但“记忆接受已提交、派生投影或请求回执尚未完成”时如果进程退出，重试会重新生成候选；现存记忆可能使候选被判为 duplicate，重试随后返回 `ignored`，而不是恢复原请求的 `memory_id`。这不是数据库内重复行问题，而是两个本地事实源之间缺少同一输入身份。

## 公开实践对照

- [LangMem](https://github.com/langchain-ai/langmem) 将长期记忆的抽取、整合和更新作为可持久化的后台管理过程；本地只借鉴“管理记录必须可重放”的边界，不引入 LangGraph 或在线自动抽取。
- [Mem0](https://github.com/mem0ai/mem0) 的记忆更新流程区分新增、更新和历史，说明记忆写入不能只依赖一次即时向量写入；本地沿用现有审核、版本和 outbox 事实源。
- [Restate durable execution](https://docs.restate.dev/foundations/key-concepts) 与 [Dapr transactional outbox](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-outbox/) 都强调恢复时应复用已提交步骤/事件的稳定身份；本地把这个原则收窄为单机两个 SQLite 存储之间的输入绑定，不宣称跨存储 exactly-once。

## 实际改动

1. 用 `session_id + request_id` 的哈希生成可打印、长度受限的 `runtime-memory` 审核幂等键，避免把原始会话内容放进键或日志。
2. 用同一条消息、项目和会话生成输入摘要；重试时先按审核键和摘要回放已有 `reflection_review`，再做候选安全门，避免已接受记忆被重新判成 duplicate 后丢失原 `memory_id`。
3. 首次执行仍沿用原有“显式偏好才自动保存、失败不写向量”的策略；审核记录、接受态记忆、派生同步和请求回执的先后边界未被伪装成原子事务。
4. 回放的 pending review 会继续走原有 `decide_review`，已完成 review 会走已有 decision digest 回放；派生投影失败时下一次请求可以继续投影并闭合会话回执。

## 失败优先与验收

- 新增 `test_runtime_memory_retry_replays_review_after_projection_failure`：第一次在记忆接受后故意让派生投影失败，第二次使用相同输入重试；结果为第一次 `500`、第二次成功，`reflection_reviews` 只有 `1` 条，active memory 只有 `1` 条。
- 运行时记忆专项 `5 passed, 1 warning`。
- 学习存储、学习 API、会话存储、记忆运行时和记忆门组合 `247 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1381 passed, 1 warning`；前端生产构建通过，8 组 UI 测试合计 `61 passed`；`compileall` 和 `cargo check --locked` 通过。

## 边界

这只闭合了运行时记忆请求在本机两个 durable 记录之间的重试身份，不是跨数据库事务，也不是外部向量服务或模型调用的 exactly-once。投影仍可能在客户端不再重试时保持 pending，由已有 outbox 回放处理；自动记忆抽取质量仍需要独立冻结数据集和人工/逐样本评测。
