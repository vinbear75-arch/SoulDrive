# 记忆写入冲突失效优化报告

日期：2026-09-10

## 目标

解决“审核候选已经标记为 `conflicts_with`，但人工点击接受后仍把旧事实和新事实同时保留为 active”的问题。这个问题会绕过读取门，增加后续检索、版本管理和用户理解的成本。

## 外部实践对照

- [Mem0 官方记忆更新示例](https://github.com/mem0ai/mem0/blob/main/docs/cookbooks/essentials/building-ai-companion.mdx)在用户目标变化时读取旧记忆并执行 update，而不是无条件新增一条记忆。
- Mem0 的[ADD-only 冲突 issue](https://github.com/mem0ai/mem0/issues/5867)复现了旧偏好与新偏好并存导致检索歧义的风险；这与 SoulDrive 审核接受后的行为一致。
- [Graphiti edge operations](https://github.com/getzep/graphiti/blob/main/graphiti_core/utils/maintenance/edge_operations.py)将 contradicted facts 作为失效候选，并以时间字段保留历史。
- Graphiti 的[失效误伤 issue](https://github.com/getzep/graphiti/issues/1728)同时说明，不能只凭宽泛语义相似度自动退休事实；范围和实体约束必须先成立。

因此本轮没有引入 Mem0/Graphiti，也没有让模型自动猜测矛盾关系；只在候选已经由写入审核明确给出 `conflicts_with`、且旧记忆与新记忆的 subtype、scope、project_id、标题均精确匹配时执行失效。

## 发现的缺口

`validate_learning_candidates` 会发现冲突并把 ID 放入候选，但审核候选持久化时此前只保留了基本文本和 `write_gate`，`decide_review` 也没有对旧记忆做任何状态变更。于是人工接受后：

- 新事实被插入为 active；
- 旧事实仍为 active；
- 旧事实的 `learning_chunks` 仍可检索；
- 派生文件同步层不会收到旧事实撤销事件。

## 最小实现

- 候选加密 payload 持久化 `conflicts_with` ID，保证审核重放时仍使用原始冲突关系。
- `decide_review` 在同一 SQLite 事务中先执行受限 supersession：
  - 只处理 active memory；
  - subtype 必须相同；
  - scope 和 project_id 必须完全相同；
  - 标题必须与未编辑候选相同；标题被人工改写时不自动失效旧事实；
  - 旧 memory、learning chunk 和历史版本一起标记为 archived。
- 旧记忆通过已有 `learning_sync_outbox` 排入 `memory_revoke`，新记忆照常排入 `memory_update`；事务失败时两者一起回滚。
- 新记忆返回 `superseded_memory_ids`，便于 UI/API 和审计知道发生了什么；重放仍从持久化 payload 返回同一结果。
- 跨项目、跨 scope 或不匹配 subtype 的冲突 ID 被忽略，避免全局偏好覆盖项目事实，也不信任客户端直接提供的 ID 作为授权。

## 测试证据

- 先写回归测试：旧实现接受新候选后没有 `superseded_memory_ids`，旧 memory 仍为 active，测试失败。
- 新增 `tests/test_learning_store.py::test_accepting_explicit_conflict_supersedes_same_scope_memory`，修复后通过。
- 学习存储、聊天学习 API、读取门专项：`112 passed, 1 warning`。
- 仓库级 `scripts/check.ps1`：后端 `1064 passed, 1 warning`；Python compileall 通过；前端生产构建通过；7 组前端测试 `48 passed`；Tauri `cargo check --locked` 通过。
- 前端 Vite 仍提示单 chunk 约 `576.12 kB`；Starlette/httpx 弃用提示仍是环境警告，不是本轮失败。

## 边界与未完成项

- 这是“审核已明确冲突关系”的安全失效，不是自动实体解析、通用自然语言矛盾检测或图谱级事实推理。
- 如果候选没有可靠的 `conflicts_with`，系统不会自行把相似记忆删除；读取门的同标题保守裁决仍是最后一道保护。
- 当前 supersession 是本机 SQLite 事务 + 本地 outbox，不是跨文件系统/外部服务 exactly-once；派生同步仍需要已有重放和租约机制。
- 真实 Tauri/WPS、安装包、模型/视觉服务和冻结留出集上的答案质量仍需独立验收。
