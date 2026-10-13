# 会话列表确定性排序优化

## 结论

会话清单在多个会话具有相同 `updated_at` 时，原实现只按时间排序，结果会依赖文件系统的 `glob()` 枚举顺序。对于研究窗口、实验对比窗口和会议行动窗口，这会让刷新后的默认候选顺序漂移，增加重现和回放成本。

本轮将会话列表固定为 `updated_at DESC, session_id ASC`。会话 ID 只作为同一更新时间的末级稳定键，不改变最近更新优先级，也不改变项目/全局过滤。

## 外部实践与本地映射

- [Open WebUI 的聊天列表查询](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/models/chats.py) 在 `updated_at` 后追加 `Chat.id`，并把该键用于稳定列表/分页。
- [LangGraph 的持久化模型](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx) 将 `thread_id` 作为短期会话状态的持久身份；SoulDrive 沿用本地会话 ID 作为最终确定性键，没有引入外部线程服务。

## 失败优先与修复

- 失败回归先把两个项目会话的 `updated_at` 固定为相同值，并让目录枚举返回 `session-b`、`session-a`；旧实现按枚举顺序返回，无法保证稳定 ID 顺序。
- `core/conversation_store.py::list_sessions()` 改为时间倒序、会话 ID 升序。
- 新增 `tests/test_conversation_store.py::test_session_list_uses_session_id_as_stable_tie_breaker`，同时覆盖反向枚举反例。

## 已验证

- 会话存储与 MCP 学习/聊天入口组合：`112 passed, 1 warning`。
- 全量 `scripts/check.ps1` 已执行：后端 `1200 passed, 1 warning`；前端生产构建通过，7 组测试共 `49 passed`；Tauri `cargo check --locked` 通过。构建仅保留既有的单个 JavaScript chunk 体积提示。

## 边界

- 该改动只保证会话清单的排序可重放，不替代前端显式选择，也不改变不同 `workflow_id` 会话的功能边界。
- 该改动不证明真实 Tauri UI、浏览器交互、WPS 或打包安装流程已完成验收。
