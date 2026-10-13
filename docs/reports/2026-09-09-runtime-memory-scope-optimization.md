# SoulDrive 自动记忆候选的项目范围优化

日期：2026-09-09

## 结论

自动记忆旁路在判断“重复/冲突”时现在先按当前会话的项目范围读取已有记忆，再执行候选校验。此前该入口使用未过滤的全量记忆列表：项目 A 的同名/同内容记忆可能让项目 B 的合法候选被错误判为 duplicate，或者让全局模式看到项目事实。

本轮只修正候选校验的读取范围，不改变最终记忆的项目写入规则，也没有把不同项目的记忆合并。

## 外部实践对照

- [Qdrant filtering](https://qdrant.tech/documentation/concepts/filtering/)要求把租户/范围条件作为检索过滤的一部分，而不是召回后再凭结果猜测；SoulDrive 将同一原则用于本地记忆候选的 duplicate/conflict 检查。
- [Open WebUI memory 文档](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/memory.mdx)将记忆默认绑定到用户范围，并将长期记忆的读写作为独立管理入口；SoulDrive 进一步按项目策略区分全局偏好与项目事实。
- [Graphiti MCP 说明](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py)把 group/context 作为事实图的边界；本轮不引入图数据库，只保证自动记忆的候选判断不跨项目读取。

## 失败优先复现

- 在项目 A 先保存 `project_a_preference_token`。
- 在项目 B 提交同内容的显式用户偏好候选。
- 旧实现把项目 A 记忆放入 `existing_assets`，返回 `ignored`；期望行为是项目 B 独立保存自己的项目事实。

## 当前改动

`core/mcp_server.py` 的 `/learning/memories/runtime`：

- 使用当前工作区的 `memory_policy` 计算 `include_global`。
- 始终以 `context_only=True` 构造候选校验输入：有项目时只读当前项目加允许的全局记忆；没有项目时只读全局记忆。
- 最终创建 review 和持久化仍沿用请求的 `project_id`，所以 duplicate 检查范围与写入范围一致。

## 验证

- 旧实现回归失败：`runtime_memory_duplicate_detection_is_project_scoped` 得到 `ignored` 而不是 `saved`。
- 修复后该专项：`1 passed, 1 warning`。
- 前一轮记忆/学习 API 专项：`80 passed, 1 warning`（绑定审核草稿后的完整专项）。
- 本轮新增测试覆盖项目 A 与项目 B 的同内容候选不互相阻断；随后仓库级完整门通过后端 `1019 passed, 1 warning`，前端构建与 `44 passed`、Tauri `cargo check --locked` 均通过。

## 未完成边界

- 该门解决的是项目范围，不是语义事实合并；同一项目内的冲突仍需人工审核和有限读取择优。
- 全局 `user_preference` 仍可按项目策略注入；`project_only` 仍明确排除全局记忆。
- 这是本机 SQLite 记忆范围校验，不等于跨主机多租户授权系统。
