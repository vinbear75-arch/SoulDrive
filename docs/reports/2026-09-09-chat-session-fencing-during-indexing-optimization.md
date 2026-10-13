# 索引状态切换期间的聊天会话范围 fencing 优化记录

日期：2026-09-09  
范围：`/chat` 的会话项目/工作流绑定、索引忙状态和流式执行竞态

## 结论

本轮发现一个由索引状态切换触发的会话边界竞态。旧实现为了在索引忙时尽快返回，使用 `request.session_id and not indexing_busy` 作为会话元数据读取条件：请求首读到索引正在运行时跳过持久会话校验；如果索引在真正创建流式生成器前恢复空闲，后续链路会直接采用客户端的 `active_context.project_id` 和请求级 workflow，绕过 session 的持久项目/角色绑定。

现在只要请求携带 `session_id`，就必须在进入等待或执行路径前读取会话元数据；项目冲突、角色冲突、不存在会话和会话存储不可用均在 HTTP 层终止，不能依赖索引忙状态作为绕过条件。无 `session_id` 的普通请求仍可保留原有索引等待行为。

## 外部实践对照

- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)把 `thread_id` 作为持久状态的主键；执行和恢复都必须用同一线程标识，不能在恢复时让调用方重新定义线程上下文。
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)要求在中断后用同一 `thread_id` 恢复，并把 checkpoint 作为继续执行的依据。SoulDrive 当前不引入 LangGraph，而是把相同原则应用到本地 `ConversationStore`：先锁定 session 的项目/工作流，再决定是否等待索引或开始 RAG。
- [Open WebUI auth utility](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/utils/auth.py)在请求进入业务处理前解析并保存已验证身份；SoulDrive 的单机边界不需要用户 RBAC，但不能把请求体中的项目字段当作会话授权替代品。

## 实现

- `core/mcp_server.py`
  - 移除 `not indexing_busy` 条件；带 `session_id` 的请求在索引忙时也读取会话元数据。
  - 会话存储为空或不可用时 fail closed 返回 `503`；未知会话返回 `404`；项目/工作流不匹配返回 `409`。
  - 通过后才允许后续的索引等待、幂等回执和流式 RAG 路径继续处理。
- `tests/test_multimodal_workflows.py`
  - 新增故障注入回归：首读索引状态为 `indexing`，执行阶段读为 `idle`；旧代码会让客户端项目越界并调用 fake RAG，修复后返回 `409` 且 fake RAG 未调用。

## 验证

- 旧代码：新增回归为 `200`，且 fake RAG 被调用，证明竞态可达。
- 修复后：新增回归 `1 passed, 1 warning`。
- `tests/test_multimodal_workflows.py`：`26 passed, 1 warning`。
- `tests/test_mcp_server_indexing.py`：`11 passed, 1 warning`。
- 仓库级 `scripts/check.ps1`：后端 `1045 passed, 1 warning`；前端生产构建与 7 组测试合计 `45 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这只修复本地单机 session 项目/工作流绑定在索引状态切换期间的 fencing，不实现跨主机分布式 session 锁。
- 索引忙时仍不会执行 RAG；本轮只保证状态恢复竞态不能跳过会话校验。
- 真实索引 worker、Tauri UI 和模型质量不由 fake RAG 回归证明；仍需按执行证据中的真实环境门单独验收。
