# PPT 任务项目范围优化记录

日期：2026-09-09  
范围：PPT 大纲/生成任务的列表、读取、确认、恢复、取消，以及前端任务状态轮询

## 结论

本轮发现并修复一个真实的项目边界缺口：PPT 任务创建时已经把 `project_id` 写入持久化任务输入，但任务控制接口只按全局 `task_id` 读取或改变状态。只知道任务 ID 的调用方可以在不提供项目范围时读取大纲，也可以尝试确认、恢复或取消其他项目的任务。

现在项目绑定的 PPT 任务必须在详情、确认、恢复和取消请求中携带完全匹配的 `project_id`；缺少范围或跨项目范围均返回 `409`。任务列表不带项目参数时只返回工作区级任务，带项目参数时只返回该项目任务。创建接口的公开任务响应也明确返回 `project_id`，前端据此把范围带入轮询和所有任务操作。

## 外部实践对照

- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)把持久化状态绑定到显式 `thread_id`，并要求每次读取 checkpoint 时提供相同线程上下文；SoulDrive 对 PPT 任务采用同一原则，但以更小的 `project_id + task_id` 范围门实现。
- [Open WebUI knowledge filesystem](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/tools/knowledge_fs.py)在解析知识资源前先校验 owner/admin/access grant，而不是把资源 ID 当作授权本身。
- [Open WebUI 知识检索越权公告](https://github.com/open-webui/open-webui/security/advisories/GHSA-4g37-7p2c-38r9)记录了“只验证资源 ID、未验证访问范围”会导致跨知识库读取；[后台知识库 ID 越权公告](https://github.com/open-webui/open-webui/security/advisories/GHSA-7r7x-gjvr-448g)进一步说明后台任务也必须重复执行授权检查，不能因为任务已入队就跳过范围验证。
- [Open WebUI 数据库设计](https://github.com/open-webui/docs/blob/main/docs/reference/database-schema.md)将知识资源与授权关系分开建模。SoulDrive 当前是本机单用户产品，不引入 RBAC，而是把已有项目归属字段变成每次任务操作都要满足的显式约束。

## 实现

- `core/task_journal.py`
  - `public_task()` 暴露任务输入中的 `project_id`，让客户端可以携带持久化的真实范围，而不是从当前界面猜测。
- `core/mcp_server.py`
  - 新增通用 `_project_scope_response`，并由 PPT 任务复用。
  - `GET /presentations/tasks/{task_id}`、`POST /confirm`、`POST /resume`、`POST /cancel` 在任何状态判断或副作用前执行精确项目匹配。
  - `GET /presentations/tasks` 不带项目参数时排除项目任务；带参数时只返回完全匹配的项目任务。
- `souldrive-ui/src/App.tsx`
  - `PresentationTask` 保留 `project_id`。
  - 任务轮询、确认、停止和从检查点恢复均从当前持久化任务构造 `project_id` 查询参数，并将其作为轮询依赖的一部分。
- `tests/test_mcp_server_presentations.py`
  - 先证明旧实现会在无范围详情请求上返回 `200`，再验证列表、详情、恢复、确认和取消的缺失/错误/正确范围行为。
  - 验证创建与确认返回的公开任务确实携带 `project_id`。
- `souldrive-ui/scripts/document-scope.test.mjs`
  - 增加静态回归，确认前端四类任务请求均拼接持久化项目范围。

## 验证

- 旧实现回归：无 `project_id` 读取项目 PPT 任务返回 `200`，目标为 `409`；跨项目请求同样未被拦截。
- 修复后定向回归：`tests/test_mcp_server_presentations.py tests/test_presentation_service.py` 为 `22 passed, 1 warning`。
- 前端：`npm run test:document-scope` 为 `13 passed`；`npm run build` 通过。
- 仓库级 `scripts/check.ps1`：后端 `1045 passed, 1 warning`；前端构建通过，7 组测试全部通过；Tauri `cargo check --locked` 通过。

## 边界

- 这不是多用户 RBAC，也不把本机单用户应用扩展成云端租户系统；它只保证现有项目字段不会在任务生命周期的后续操作中被全局 ID 绕过。
- 任务列表无项目参数时只显示工作区级任务；若未来产品需要跨项目管理员视图，应先设计并评测显式的审计/管理范围，不能恢复“无参数返回全部项目任务”。
- 这项范围门不等于 PPT 内容正确性；证据覆盖、事实引用、渲染质量和真实 WPS 打开仍由既有 PPT 评测与人工审核负责。
