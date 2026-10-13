# 记忆来源会话 provenance 优化

日期：2026-09-10

## 结论

本轮补齐了长期记忆来源追踪的一个实际歧义：原记忆只保存 `source_message_indexes`，却没有保存这些序号属于哪个会话。两个会话都存在第 0 条用户消息时，记忆卡片只能显示一个无法唯一定位的序号。

现在 `/reflection/draft` 接受可选 `session_id`。服务端会读取该会话的持久化项目绑定：未显式传入 project 时沿用会话项目；显式项目与会话项目冲突时返回 `409`，不会生成候选。来源会话 ID 会沿着审核候选、正式 memory、历史版本、关键词检索和 memory capsule 的 `source_trace` 传递。旧客户端不传 `session_id` 时保持兼容，旧数据仍按原有 candidate/message-index provenance 展示。

## 外部实践对照

- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 用 episode UUID、source description 和 group scope 追踪派生事实的来源，并把时间/来源过滤作为查询契约的一部分。
- [Graphiti repository](https://github.com/getzep/graphiti) 将 episodes 描述为派生事实的 ground-truth stream，强调事实应能回溯到原始输入，而不是只保留扁平文本。
- [LangMem repository](https://github.com/langchain-ai/langmem) 将跨会话长期记忆作为显式持久化对象管理；SoulDrive 采用更小的本地实现，只增加现有候选 payload 的来源字段和会话范围校验，没有引入新的记忆服务。
- [Letta archival memory](https://github.com/letta-ai/letta/blob/main/letta/functions/function_sets/base.py) 强调归档记忆应是可检索的自包含事实/摘要；会话 ID 作为 trace metadata，补足定位而不把原始对话全文注入长期记忆。

## 失败优先验证

- 新增 `LearningStoreTests.test_memory_preserves_source_session_in_provenance`：验证正式 memory、列表、关键词搜索都返回来源会话。
- 新增 `McpServerLearningTests.test_reflection_draft_binds_source_session_to_memory_provenance`：验证 API 从会话推导 project scope，并让 `accepted_assets` 立即带完整 `source_trace`。
- 新增 `McpServerLearningTests.test_reflection_draft_rejects_source_session_project_mismatch`：验证跨项目来源会话在候选生成前被拒绝。
- 实现前 API 回归发现正式资产响应缺少 `source_trace`；修复后来源存储/检索和 API 隔离回归共 `3 passed, 1 warning`，此前存储/运行时相关组合为 `91 passed`。

## 实现

- `ReflectionDraftRequest` 增加可选 `session_id`，反思草稿幂等摘要同时绑定来源会话。
- `reflection_draft` 读取持久化 session metadata，校验项目边界，并把服务端确认的 session ID 写入所有已验证候选。
- `LearningStore` 在加密候选 payload、memory、memory version 和 search record 中保留 `source_session_id`；`source_trace` 增加 `session_id`。
- memory 读取 capsule 的 provenance 后缀显示来源会话标识；不显示来源原文，不扩大模型上下文范围。
- 前端反思草稿请求传入当前活动会话 ID。

## 边界

- 这不是把客户端消息索引变成消息内容证明；客户端仍需提供反思 transcript，且现有 evidence/source-index 校验继续生效。
- 来源会话删除后，已沉淀的 memory 仍保留 ID 级 provenance，但本轮没有自动级联删除长期记忆；删除/撤销仍由现有人工审核与显式管理接口负责。
- 没有新增 episodic memory 数据库。会话本身仍由 ConversationStore 保存，长期 memory 只持有可定位的来源引用；只有出现可复现的查询需求后再增加跨会话事件召回。
