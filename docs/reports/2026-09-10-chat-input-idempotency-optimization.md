# SoulDrive 聊天请求完整输入幂等优化记录

日期：2026-09-10

## 本轮结论

聊天请求收据不再只按 `query` 的哈希绑定。带 `request_id` 的 `/chat` 请求现在会把有效项目、会话角色、Top-K、时间戳、显式历史、感知事件和活动选区纳入规范输入摘要：

- 同一个 `request_id`、同一份输入可以正常重放已完成回答；
- 同一个问题但感知事件/活动选区变化，会返回 `409`，不会错误重放旧回答；
- 服务端自动加载的历史不纳入摘要，因为首轮请求持久化后历史必然增长，不能让合法重试因派生状态变化失效。

这使本机的请求收据更接近“幂等键绑定完整调用输入”的语义，同时仍明确保留本地模型调用的 at-least-once 边界：模型本身不是外部 exactly-once 事务。

## 外部实践依据

- Restate 文档把幂等键作为服务调用的稳定身份，并保存调用结果；相同键重复调用应重放，相同键携带不同输入应被视为冲突：[Restate invocations](https://docs.restate.dev/services/invocation/http)、[Restate key concepts](https://docs.restate.dev/foundations/key-concepts)。
- LangGraph 持久化文档区分线程 checkpoint 与长期记忆 namespace；SoulDrive 本轮对应地把会话项目/角色作为服务端有效边界，把客户端显式上下文纳入调用摘要：[LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。

本轮没有引入外部运行时或分布式队列；继续复用已有 SQLite 本机请求收据、租约和加密会话归档。

## 实现范围

1. `ConversationStore.claim_request` 接受可选规范 `input_digest`；未提供时保留旧的 query-only 兼容路径。
2. `ProductionSessionStore` 透传该摘要，保持现有租约、接管、完成和失败状态机不变。
3. `core/mcp_server.py` 新增 `_chat_input_digest`，在会话项目/角色解析和感知事件范围校验后计算摘要，并在请求收据认领时传入。
4. 摘要只包含客户端显式历史；不把请求执行期间由服务端载入并随后增长的会话历史当成幂等输入。

## 验收证据

- 先添加失败测试：旧 `ConversationStore.claim_request` 不接受 `input_digest`；旧 `/chat` 对相同问题、相同 `request_id` 但不同选区仍返回缓存 `200`。
- 修复后通过：
  - `test_request_receipt_binds_same_query_to_full_input_digest`
  - `test_chat_request_id_rejects_same_query_with_changed_perception_context`
  - 同一测试同时验证相同完整输入重放为 `200`。
- 定向幂等回归：`2 passed, 1 warning`；聊天完成回放兼容回归：`2 passed, 1 warning`。
- `tests/test_mcp_server_learning.py tests/test_conversation_store.py tests/test_interaction_context.py` 的完整会话/上下文回归：`107 passed, 1 warning`。
- 随后的仓库级 `scripts/check.ps1`：后端 `1060 passed, 1 warning`；Python compileall、前端构建、7 组 UI 测试 `46 passed`、`cargo check --locked` 均通过。

## 边界与后续

- 输入摘要能阻止“同键不同输入”的错误重放，但不能使本地模型、文件系统或外部邮件发送获得 exactly-once；外部副作用仍必须使用各自的确认令牌、幂等键和持久状态机。
- 历史请求收据若来自旧版本且只有 query 摘要，升级后无法推断其旧上下文；后续重试以保守冲突为安全边界，不静默猜测其感知上下文。
- 下一轮继续审计其他可产生副作用的入口是否都绑定了完整输入摘要和项目范围，而不是只在聊天入口补强。
