# 记忆编辑版本历史优化记录

日期：2026-09-09  
范围：`LearningStore` 的记忆编辑、时间点列表、关键词搜索和删除/归档边界

## 发现

已有 `valid_from` / `invalid_at` 和 `as_of` 查询，但 `update_memory` 会原地覆盖 `memories` 与 `learning_chunks` 的加密内容。这样时间过滤看似存在，历史内容却已经丢失：旧时点只能返回空结果，或者在未正确设置区间时误读新内容。

这与 Graphiti/Zep 的核心做法不一致：事实被新信息失效时，旧事实仍保留 `valid_at` / `invalid_at`，检索可以按时间范围分析。参考：[Zep Facts](https://help.getzep.com/facts)、[Graphiti Adding Episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes)、[Zep Searching the Graph](https://help.getzep.com/searching-the-graph)。

## 最小实现

- `core/learning_store.py`
  - 新增加密 `memory_versions` 表，按 `memory_id + record_version` 保存旧 payload、来源、项目范围、使用时间和有效区间。
  - 内容编辑时将旧版本的 `invalid_at` 截止到新版本 `valid_from`；未显式提供时间时，内容变更默认以编辑时刻作为新版本起点。
  - `list_memories(context_only/as_of)` 与 `search(context_only/as_of)` 同时读取当前版本和有效的历史版本，并按 `memory_id` 去重。
  - 删除/归档同步隐藏历史版本，避免删除的记忆在历史查询中重新成为活动记忆。
- `tests/test_learning_store.py`
  - 新增编辑前后时点回溯回归：`as_of=150` 只能命中旧内容，`as_of=250` 只能命中新内容；同时覆盖列表和关键词搜索。

## 验证

- 新增回归在实现前失败：旧内容历史搜索返回 `[]`，实现后通过。
- `tests/test_learning_store.py`：`36 passed`。
- `tests/test_temporal_memory.py tests/test_mcp_server_learning.py tests/test_rag_prompting.py`：`121 passed, 1 warning`。

## 边界

- 这是本地记忆的版本化与时点检索，不是 Graphiti 式实体/关系图，也不做自动语义冲突合并。
- 版本历史仍以用户显式编辑和已有有效区间为依据，不从自然语言自动推断复杂事件时间。
- 全量检查、前端、Tauri 和真实模型质量仍需按总执行证据分别验证。
