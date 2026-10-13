# 2026-09-14 Durable outbox 与会话身份一致性优化

日期：2026-09-14  
范围：学习记忆同步 outbox、跨后端控制 outbox、会话消息身份、GraphRAG 原始证据门和会话摘要 checkpoint。

## 1. 对照的公开机制

- [durable-sync](https://github.com/isaacrowntree/durable-sync) 将不可变 `opId`、幂等应用和客户端 outbox 分开，说明事件身份不能只依赖当前聚合状态。
- [Tandem transactional outbox](https://github.com/alirux/tandem) 用聚合序号和事件类型保留同一聚合的顺序，避免只用聚合 ID 覆盖不同事件。
- [pg-outbox](https://github.com/czeresniowski/pg-outbox) 明确 outbox relay 是 at-least-once，消费端仍必须以稳定事件身份去重；本地投影因此继续采用版本/状态 fence，而不声称 exactly-once。

这些项目只提供机制参考；本轮没有引入新的消息中间件或外部数据库。

## 2. 本轮真实缺口与修复

1. **控制 outbox 丢失记忆投影身份**：Redis 向量投影的跨后端控制事件原先没有保留 `scope/status/valid_from/invalid_at/workflow_id/memory_subtype`，重放会丢项目/时间/窗口过滤，`workflow_fact` 还可能因缺少 workflow 而永久失败。现在控制事件保留完整作用域、有效性、版本和 workflow 身份。
2. **后端事件键只含聚合 ID**：同一 session 的 `created/summary/deleted` 事件会互相覆盖。通用后端事件键现在包含事件类型；记忆事件继续包含 record version 和状态，兼容原有精确 ID 断言。
3. **摘要 checkpoint 可被迟到旧版本回退**：摘要写入现在遵循已提交版本单调递增；旧版本重放返回当前最新摘要，同版本保留首写者结果。
4. **学习记忆 outbox 的值幂等缺口**：同一 `operation + memory_id + record_version` 冲突时，原 `INSERT OR IGNORE` 会静默接受旧载荷。现在相同载荷稳定重放，载荷冲突明确失败，不再把不同内容伪装成重试。
5. **会话消息 ID 未绑定 session**：原请求消息 ID只由 `request_id + role + status` 组成，同一 request key 在两个会话中会误命中第一个会话。新消息身份绑定 `session_id`；旧归档会先按本会话 request/role 回读以保持重试兼容；显式 message ID 跨会话或载荷冲突均拒绝。
6. **GraphRAG 派生报告可被适配器误当原文证据**：Context Gate 现在对 `derived_index=true` 证据 fail-closed。GraphRAG 社区报告仍只作导航上下文，最终事实证据必须回读原始 chunk。

## 3. 验证证据

- 会话事实源全量：`59 passed`。
- 本轮 durable/RAG/GraphRAG/TaskJournal 组合：`362 passed, 1 warning`。
- 全工程 `scripts/check.ps1`：后端 `1486 passed, 1 warning`；前端生产构建通过；8 组 UI 脚本合计 `62 passed`；Python `compileall` 通过；`cargo check --locked` 通过。
- 已有确定性评测仍通过：记忆质量 `7/7` 场景、`8/8` 查询；GraphRAG 路由 `4/4`；情景工作流路由运行时 `5/5`，baseline `2/5`。

## 4. 明确边界

本轮证明的是本机单用户服务内的事件身份、版本/状态迟到保护、会话隔离和 GraphRAG 证据边界；不证明跨进程/跨机器 exactly-once、Redis/PostgreSQL 实例级联调、身份认证、外部 SMTP/日历、真实 Tauri/WPS UI/E2E 或模型答案准确率。outbox 仍是 at-least-once，投影处理器必须保持幂等；`1486 passed` 也不等于打包启动验证。
