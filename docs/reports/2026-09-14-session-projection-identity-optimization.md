# 会话投影身份冲突优化

日期：2026-09-14

## 发现

本地会话 manifest 已把 `session_id`、项目、标题和研究窗口固定为 durable 身份，但 Redis `set_session_metadata()` 与 PostgreSQL `upsert_session()` 仍把同一 session 的冲突元数据当作普通更新。旧 outbox 延迟回放或错误事件因此可能把投影改成另一项目/窗口，造成事实源与派生会话目录不一致。

## 公开实践对照

- [LangGraph checkpoint persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx)：持久状态通过 thread/checkpoint identity 关联，历史身份不能被临时更新覆盖。
- [LangGraph checkpoint base](https://github.com/langchain-ai/langgraph/blob/main/libs/checkpoint/langgraph/checkpoint/base/__init__.py)：`thread_id` 是保存、恢复和时间旅行的主键。
- [Restate optimistic concurrency and idempotency](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)：输入/身份冲突应产生可操作错误，而不是静默覆盖。

本轮只加强现有 Redis/PostgreSQL 派生投影的一致性，不引入新存储或分布式锁服务。

## 实现

- Redis 元数据写入先读取并解密现有 session；项目、标题或 workflow 不一致时返回 `session metadata conflict`，同身份写入保持幂等。
- PostgreSQL 写入先在同一连接内锁定现有 session；冲突立即拒绝。无现有行时使用带身份条件的 `ON CONFLICT` 更新，竞争插入返回零行后再次读取并验证，避免并发覆盖。
- 项目、标题和 workflow 的身份比较集中到同一规范化 helper；空项目仍与具体项目区分，默认标题统一为“新对话”。

## 验收

- 失败优先新增 Redis/PostgreSQL 两条冲突反例；修复前均会放行，修复后均拒绝且保留已有投影。
- `tests/test_conversation_store.py`：`67 passed`。
- 本轮完整工程门禁已通过：后端 `1501 passed, 1 warning`；前端生产构建及 8 组脚本通过；`compileall` 与 `cargo check --locked` 通过。边界仍包括未连接真实 Redis/PostgreSQL 实例联调、跨进程 exactly-once 和真实 Tauri/WPS UI/E2E。

## 边界

这保证的是派生会话投影不背离本机 manifest 的身份契约，不等于认证授权、跨存储原子提交或外部服务的事务保证。
