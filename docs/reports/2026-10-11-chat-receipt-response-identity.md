# 聊天完成回执的响应身份闭合

日期：2026-10-11

## 背景

本轮继续按已批准的固定版本实践检查本地聊天 durable replay：Temporal 的 Workflow ID/Run ID 分离、MLflow 的运行 lineage，以及 Graphiti 的 episode/group 来源身份都共同指向同一个边界——恢复或回放不能只相信一个可变的结果指针，结果必须重新绑定到当前请求的身份和完成状态。

## 失败优先

新增反例 `test_chat_does_not_replay_response_message_from_another_session`：为会话 A 创建一个已完成的请求回执，但把 `response_message_id` 指向会话 B 的 assistant 消息；两条消息使用同一 `request_id` 和输入摘要，以排除仅凭摘要即可区分的假安全。

旧实现按全局 `message_id` 取回消息后直接回放，反例返回 `200` 并泄露会话 B 的回答；B0 结果为 `1 failed, 148 deselected, 1 warning`。

## 最小修复

在 `/chat` 的 completed receipt 分支，回放消息必须同时满足：

- `session_id` 等于当前请求会话；
- `request_id` 等于当前请求；
- `role=assistant`；
- `metadata.status=completed`。

不满足时丢弃消息，并只允许按当前 `session_id + request_id + role + status` 做有界兼容回查；找不到合法响应时返回 `503 response archive is unavailable`，不加载 RAG、不生成新回答。旧的“没有精确消息 ID”的兼容路径仍保留同样的身份校验。

## 验证

- 定向回归：跨会话反例与既有完成回放 `2 passed`。
- 会话存储与学习接口交叉回归：`244 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1763 passed, 1 warning`，用时 `443.34s`。
- 冻结质量门：memory `7/7`、GraphRAG route `4/4`、workflow roles `6/6`。
- 前端生产构建通过；8 组 UI 脚本共 `66 passed`；Python compileall 与锁定 Cargo 检查通过。

## 边界

本轮证明的是本机 SQLite/加密回执在聊天重放入口的请求—响应身份闭合，不是跨主机 exactly-once 或跨存储事务提交证明；也不代表模型答案质量、真实 Redis/PostgreSQL、断电恢复或真实 Tauri/WPS UI/E2E 已完成验收。
