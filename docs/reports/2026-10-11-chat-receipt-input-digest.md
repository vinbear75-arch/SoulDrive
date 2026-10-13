# 聊天回执的输入摘要绑定

日期：2026-10-11

## 背景

在上一轮补齐 session/request/role/status 后，继续按 Temporal 的重放身份、MLflow 的运行 lineage 和 Graphiti 的来源 provenance 检查回执是否绑定了“同一个输入”。仅有相同会话和 request_id 仍不足以证明响应属于这次请求。

## 失败优先

新增反例 `test_chat_does_not_replay_response_with_stale_input_digest`：同一 session、同一 request_id 下，构造 `status=completed` 的 assistant 消息，但写入不同的 `metadata.input_digest`，并让 durable receipt 指向该消息。

旧实现只校验 session/request/role/status，反例返回 `200` 并回放旧输入的回答；B0 结果为 `1 failed, 149 deselected, 1 warning`。

## 最小修复

- `ConversationStore.claim_request()` 在 completed 回执中返回已持久化的 `input_digest`。
- `/chat` 的精确消息回放和无精确消息 ID 的兼容回查，都要求 assistant 的 `metadata.input_digest` 与回执摘要一致。
- 旧回执没有摘要时保留兼容路径；一旦回执带有摘要而响应缺失或错配，则丢弃响应并返回 `503 response archive is unavailable`，不加载 RAG。

## 验证

- 摘要错配、跨会话错配、正常完成回放：`3 passed`。
- 会话存储与聊天学习接口回归：`245 passed, 1 warning`。
- 完整 `scripts/check.ps1`：后端 `1764 passed, 1 warning`，用时 `431.59s`。
- 冻结质量门：memory `7/7`、GraphRAG route `4/4`、workflow roles `6/6`。
- 前端生产构建通过；8 组 UI 脚本共 `66 passed`；Python compileall 与锁定 Cargo 检查通过。

## 边界

本轮证明的是本机 SQLite/加密回执的请求输入—响应摘要绑定，不是跨主机 exactly-once、跨存储事务提交、断电恢复、真实 Redis/PostgreSQL、模型答案质量或真实 Tauri/WPS UI/E2E 证明。
