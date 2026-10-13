# SoulDrive 自动记忆旁路幂等优化

日期：2026-09-09

## 结论

聊天成功后的自动记忆旁路现在复用本机持久化 request receipt：前端把本次聊天的 `request_id` 传给 `/learning/memories/runtime`，服务端在现有 `request_receipts` 中使用独立的 `memory:` 命名空间记录旁路执行。

同一 `session_id + request_id + 输入摘要` 的重试会重放原来的 `memory_id`；相同 request ID 换消息或项目会返回 `409`；仍在执行的旁路返回 `in_flight`，不会并行创建第二条记忆。没有 request ID 的旧客户端保持兼容，但不获得这项重放保证。

## 外部实践对照

- [LangGraph persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx)区分 thread-scoped checkpoint 与跨 thread 的长期 store，并要求状态能在中断、失败和会话恢复后继续使用。SoulDrive 将聊天 request receipt 与长期 memory 分开，但把一次旁路写入的恢复身份绑定到会话线程和请求 ID。
- [LangGraph short-term memory](https://github.com/langchain-ai/docs/blob/main/src/oss/langchain/short-term-memory.mdx)将 thread 作为同一会话交互的连续边界；本轮不把不同聊天窗口的自动记忆混为一条全局请求。
- [Restate safe ingress retries](https://github.com/restatedev/restate/blob/main/release-notes/v1.7.0.md)说明带幂等键的入口可以合并同一 in-flight 调用，避免连接丢失时重复执行；SoulDrive 复用已有 SQLite receipt/lease/fencing，不引入 Restate 服务。

## 当前改动

- `core/mcp_server.py`
  - `RuntimeMemoryRequest` 新增可选 `request_id`。
  - 旁路使用 `memory:{request_id}` 作为 receipt key，避免和同一聊天请求本身的 `request_id` 发生语义冲突。
  - 已完成 receipt 重放 saved/ignored 结果；活动 lease 返回 `in_flight`；输入摘要不一致返回 `409`。
  - 只有 receipt owner 才能 complete/fail；receipt 失败后允许过期接管，沿用已有本机 at-least-once 语义。
- `souldrive-ui/src/App.tsx`
  - `saveRuntimeMemory` 接收并提交聊天请求 ID；原聊天回答仍不等待旁路记忆写入，旁路失败不改变已完成答案。
- `tests/test_mcp_server_learning.py`
  - 增加同一 request ID 重放得到同一 memory、换输入返回 `409` 的回归。

## 验证

- 失败优先：旧实现第二次请求被当作普通重复信号返回 `ignored`；新增契约要求重放原 `memory_id`，先失败后通过。
- `tests/test_mcp_server_learning.py tests/test_conversation_store.py`：`62 passed, 1 warning`。
- 前端 `npm run test:document-scope`：`11 passed`。
- 自动记忆旁路改动后、写入边界安全修正前的仓库级 `scripts/check.ps1` 已通过后端 `1016 passed, 1 warning`、前端生产构建、7 组测试共 `44 passed`，以及 Tauri `cargo check --locked`；随后写入边界、审核草稿绑定和候选范围修正后的完整门再次通过后端 `1019 passed, 1 warning`，其余门保持通过。构建仍只有既有的 Vite 大 chunk 提示和 Starlette/httpx 弃用提示。

## 未完成边界

- 旧客户端没有 request ID 时，仍只能依赖已有内容级 duplicate/conflict 检测；它不是严格的请求级幂等。
- receipt 与记忆数据库分属两个本地 SQLite 存储；若在“记忆事务提交”与“receipt complete”之间崩溃，仍需依靠 lease 过期后的重试和现有重复检测，不能宣称跨库 exactly-once。
- 旁路只保存显式用户偏好候选，仍受敏感内容、冲突和人工审核/显式信号门约束；本轮没有放宽自动记忆写入条件。
