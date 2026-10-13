# 学习记忆全局搜索范围优化记录

日期：2026-09-10  
范围：`/learning/search`、`LearningStore.search` 的项目/全局记忆边界

## 外部实现对照

- [Mem0 memory search](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/search.mdx) 将过滤条件作为检索契约的一部分，并明确要求搜索始终带用户范围过滤，避免跨用户污染。
- [Open WebUI memory router](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/memories.py) 将记忆搜索与资源权限检查放在同一入口，搜索结果还会按相关性阈值过滤。
- [Open WebUI memory 文档](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/memory.mdx) 将记忆搜索、读取和变更都绑定到记忆资源，而不是把裸 ID 当作访问授权。

本项目是本机单用户系统，因此不照搬多租户用户 ID；对应的最小本地边界是：有 `project_id` 时只返回该项目可见的项目记忆加全局记忆，没有 `project_id` 时只返回全局记忆。

## 失败回归与根因

新增 `McpServerLearningTests.test_learning_search_without_project_id_excludes_project_memories`，先创建一条全局记忆和一条项目记忆，再调用不带 `project_id` 的 `/learning/search`。修复前回归失败：响应同时包含“全局搜索范围”和“项目搜索范围”。

根因是 API 将 `context_only` 写成 `bool(request.project_id)`。项目为空时，`LearningStore.search` 没有追加 scope 条件，因而会返回所有项目的活动记忆。

## 最小修复

`/learning/search` 现在始终以 `context_only=True` 调用存储层：

- 无项目：存储层的既有 SQL 约束只允许 `scope='global'`。
- 有项目：保留既有 `project_id` 与 `include_global` 策略，可返回当前项目及允许的全局记忆。
- 技能检索也沿用相同的范围边界，不因缺少项目参数而泄漏项目派生资产。

没有新增依赖、向量库或模型调用；这只是补上已有范围语义在 HTTP 入口的缺失传递。

## 验证

- 修复前定向失败：`1 failed, 57 deselected`，证明回归确实捕获旧行为。
- 修复后 `tests/test_mcp_server_learning.py`：`58 passed, 1 warning`。
- 本轮修改后的仓库全量 `scripts/check.ps1`：后端 `1072 passed, 1 warning`；Python compileall、前端生产构建、7 组 UI 测试共 `48 passed`，Tauri `cargo check --locked` 均通过。

## 边界

这项修复保证 API 范围隔离，不证明记忆语义相关性、答案正确率或向量召回质量；后续仍需在冻结评测集上分别测量全局记忆、项目记忆、时间点记忆和无关记忆拒答率。
