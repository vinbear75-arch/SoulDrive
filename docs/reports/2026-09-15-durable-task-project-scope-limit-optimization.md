# Durable 任务项目范围与 limit 顺序优化

日期：2026-09-15  
范围：TaskJournal、PPT 任务列表、项目概览、episodic activity view

## 外部实践与真实缺口

对照 [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) 的稳定 `thread_id`/checkpoint 边界和 [LangGraph Agent Server persistence](https://docs.langchain.com/langsmith/agent-server) 的持久任务状态查询，任务状态列表必须先落在正确的持久化范围，再做分页或 limit。

SoulDrive 原有 `TaskJournal.list_tasks(limit)` 先取全局最新 N 个任务，再由 `/presentations/tasks?project_id=...`、项目概览和 episodic 视图事后过滤。若其他项目的任务占满 N 个位置，目标项目的任务虽已持久化，却不会进入返回集；这是诊断、恢复和研究窗口活动视图的静默缺失。

## 失败优先与最小实现

新增反例：目标项目任务较旧、外部项目任务较新，`limit=1` 且指定目标项目时，旧实现先抛出不支持 project scope 的接口；修复后的查询必须返回目标项目任务。

实现保持本机加密事实源边界：

- `TaskJournal.list_tasks()` 增加 `project_id` 和 `global_only` 过滤；由于任务输入加密，先按更新时间扫描并解密最小输入范围确认项目，再对匹配任务应用 limit，返回任务时才生成完整公开投影。
- PPT 任务服务按 `presentation.plan`/`presentation.generate` 分别取目标范围，再稳定合并排序，避免非 PPT 任务或其他项目任务挤占窗口。
- PPT API、项目概览和 episodic query projection 将范围前置，不再依赖取回后过滤。

## 回归证据

- `tests/test_task_journal.py`：`27 passed`。
- `tests/test_mcp_server_presentations.py`：`6 passed, 1 warning`。
- `tests/test_presentation_service.py`：`32 passed`。
- 本轮收尾工程门禁：后端 `1553 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，Python `compileall` 与锁定 Cargo 检查通过。
- 冻结评测保持：记忆质量 `7/7` 场景、`8/8` 查询，`memory_recall_at_k=1.0`、陈旧/禁止泄漏率 `0.0`；GraphRAG 路由 `4/4`；研究窗口 runtime `5/5`（baseline `2/5`）。

## 边界

本轮证明本机 SQLite durable 任务列表不会因跨项目任务占满 limit 而静默漏掉目标项目任务；不证明跨进程 exactly-once、真实 PostgreSQL/Redis、外部副作用、打包 Tauri/WPS UI/E2E 或长时间性能。任务输入仍需解密后才能做项目过滤，超大历史任务量仍应单独做分页/索引评估。
