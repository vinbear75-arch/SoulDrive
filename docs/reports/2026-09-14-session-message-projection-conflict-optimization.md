# 会话消息投影冲突优化

日期：2026-09-14

## 发现

本地加密消息事实源已经按显式 `message_id` 校验会话、角色、正文和元数据，但 PostgreSQL 消息投影使用 `ON CONFLICT(message_id) DO NOTHING`，同一消息身份遇到不同正文时会静默忽略。这样会把“投影已存在”错误地当成“投影一致”，隐藏 outbox 损坏或错误事件。

## 公开实践对照

- [Restate safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)：幂等身份用于合并同一次请求的重试，身份相同但输入不同应返回冲突。
- [LangGraph checkpointer](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/checkpointers.mdx)：checkpoint 保存完整状态与 metadata，重放依赖稳定 identity，不能用一个 key 静默替换另一份状态。
- [pg-outbox](https://github.com/czeresniowski-dev/pg-outbox)：outbox 消费是 at-least-once，派生写入必须可去重且可发现冲突。

本轮只加强 PostgreSQL 可重建消息投影，没有把本地系统宣称为跨存储 exactly-once。

## 实现

- 写入前按 `message_id` 锁定现有行，对比 `session_id`、序号、角色、正文 hash 和消息元数据；不同则返回 `message projection conflict`，相同则安全回放。
- 无现有行时使用 `ON CONFLICT DO NOTHING`；竞争插入返回零行后重新读取并执行同一比较，避免并发路径绕过冲突门。
- 本地加密消息文件仍是事实源，PostgreSQL 继续只是派生投影；投影冲突不会改写本地消息。

## 验收

- 新增失败优先反例：同一 `message_id` 的不同正文投影必须拒绝；修复后通过。
- `tests/test_conversation_store.py`：`68 passed`。
- 本轮完整工程门禁：后端 `1502 passed, 1 warning`；前端生产构建及 8 组脚本 `62 passed`，`compileall` 与锁定 Cargo 通过。边界仍包括真实 PostgreSQL 实例联调、跨进程 exactly-once 和真实 Tauri/WPS UI/E2E。

## 边界

这保证的是消息投影不会静默接受不同输入，不替代权限校验、外部数据库事务或消息内容事实准确率。
