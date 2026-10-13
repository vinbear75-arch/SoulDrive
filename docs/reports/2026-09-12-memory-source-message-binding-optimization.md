# 记忆来源消息绑定与序号 provenance 优化

日期：2026-09-12

## 结论

本轮修复了 `source_session_id` 之后仍然存在的来源可信度缺口。此前 `/reflection/draft` 只把客户端提交的消息数组交给候选验证，再把客户端传入的消息序号绑定到真实会话 ID；客户端可以提交一段不在该会话中的文字，仍生成带真实会话 ID 的长期记忆。

现在当请求提供 `session_id` 时，服务端会从本地加密会话归档读取最近 200 条权威消息，并按角色、正文和时间顺序将客户端可见消息绑定到归档记录。任何消息无法匹配都会在候选生成前返回 `409`。通过绑定后，候选、正式 memory、历史版本、关键词搜索和 memory capsule 同时保留 `source_message_ids` 与 `source_message_sequences`；原有相对 `source_message_indexes` 继续保留以兼容旧客户端。

## 外部实践对照

- [Graphiti MCP server](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 用 episode UUID、source description 和 group scope 追踪事实来源，并把来源过滤作为查询契约的一部分。本轮采用同一类“权威事件标识”思路，但复用 SoulDrive 已有的本地消息 ID/sequence。
- [Graphiti repository](https://github.com/getzep/graphiti) 将 episode 作为派生事实的 ground-truth stream；因此 SoulDrive 不再只相信调用方提交的 transcript，而先校验它是否来自绑定会话。
- [Temporal activity state](https://github.com/temporalio/temporal/blob/main/chasm/lib/activity/proto/v1/activity_state.proto) 为每次 Activity attempt 保存 stamp，使旧尝试的回调不能冒充当前尝试。本轮没有引入 worker attempt 表，只把同一原则应用到来源消息：以归档中的 message ID/sequence 作为不可混淆的定位信息。

## 失败优先验证

- 新增 `McpServerLearningTests.test_reflection_draft_rejects_transcript_not_matching_source_session`：真实会话只保存“真实会话消息”，请求却提交另一段文字；修复前接口返回 `200` 并接受候选，回归因此先失败（`200 != 409`）。
- 修复后该反例返回 `409`，且不会创建可审核候选。
- 真实来源会话 API 回归增加 `message_id`/`sequence` 断言；学习存储和 memory capsule 回归覆盖正式 memory、列表、搜索与上下文输出。
- 学习/记忆/聊天 API 组合回归：`141 passed, 1 warning`；来源专项回归：`3 passed, 1 warning`。
- 仓库统一检查：后端 `1139 passed, 1 warning`；前端生产构建与 7 组 UI 静态测试合计 `48 passed`；Tauri `cargo check --locked` 通过。该检查仍不等同于真实 Tauri/WPS UI、安装包启动或跨进程 E2E 证明。

## 实现

- `core/mcp_server.py` 增加有限的会话消息按序绑定；角色 `ai` 规范化为 `assistant`，不把正文写入错误响应。
- `core/learning_store.py` 将权威 `source_message_ids`/`source_message_sequences` 放进已有加密 payload，并贯穿 candidate → memory → memory version → search。
- `core/memory_runtime.py` 在 provenance 后缀中显示有限数量的消息 ID/sequence，不显示原始会话正文。
- 没有新增数据库表、向量字段、Graphiti/LangGraph/Temporal 运行时或后台 worker。

## 边界

- 只对显式提供 `session_id` 的反思草稿启用权威消息绑定；旧客户端不传 session 时保持原有兼容路径，但其 `source_message_indexes` 不能被当作强来源证明。
- 绑定窗口最多读取最近 200 条消息；超过该窗口的旧消息会安全拒绝，而不是猜测来源。当前 UI 发送的历史窗口最多 32 条，处于该边界内。
- 这是本地单用户会话来源校验，不是模型生成 exactly-once，也不阻止来源会话删除后已沉淀记忆继续存在；删除/撤销仍由现有记忆管理门负责。
