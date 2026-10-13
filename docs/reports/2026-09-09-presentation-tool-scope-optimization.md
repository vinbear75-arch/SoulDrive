# PPT 工具调用路径范围优化记录

日期：2026-09-09  
范围：`/tools/call` 暴露的 PPT 生成与大纲确认工具

## 结论

本轮发现并修复一条绕过 HTTP 范围门的旁路：PPT HTTP 接口已经要求项目任务携带匹配的 `project_id`，但 `/tools/call` 的 `souldrive.presentation_confirm` 原先只接受 `plan_task_id + plan_digest`，可以在没有项目范围时确认项目任务。工具生成路径也没有复用 HTTP 对显式 `source_version_ids` 的项目验证。

现在：

- `souldrive.presentation_generate` 在项目存在性和显式来源版本范围上执行与 HTTP 入口一致的校验；
- `souldrive.presentation_confirm` 接受可选 `project_id`，项目任务缺少范围或范围错误时拒绝；工作区级任务不接受伪造的项目参数；
- 工具调用的失败发生在确认或创建任务副作用之前，避免把“工具路径”当成安全边界例外。

## 外部实践对照

- [Open WebUI knowledge filesystem](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/tools/knowledge_fs.py)在工具解析文件时重新计算可访问知识库，并校验模型绑定集合；工具调用不因拥有资源 ID 就自动获得访问权。
- [Open WebUI knowledge router](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/knowledge.py)对资源列表批量计算读/写权限，说明读取入口和写入入口都必须使用同一范围判断。
- [Open WebUI 检索越权公告](https://github.com/open-webui/open-webui/security/advisories/GHSA-4g37-7p2c-38r9)与[后台知识库 ID 越权公告](https://github.com/open-webui/open-webui/security/advisories/GHSA-7r7x-gjvr-448g)说明，检索和后台任务路径不能把客户端提供的资源 ID 当成授权结果。
- [LangGraph Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)把可恢复任务的输入和结果作为持久化执行边界；SoulDrive 继续使用本地 `TaskJournal`，并让工具路径复用同一任务范围验证，而不是新建一套隐式状态。

## 实现

- `core/mcp_server.py`
  - 把项目范围判断拆成可复用的 `_project_scope_error`，HTTP 路由返回 `409`，工具调用则转换为受控的工具参数错误。
  - `_presentation_tool_generate` 验证项目存在，并将显式来源版本限制在该项目后再创建计划。
  - `_presentation_tool_confirm` 在 `confirm_plan` 前读取任务并检查 `project_id`。
- `core/tool_registry.py`
  - `souldrive.presentation_confirm` 的输入 schema 增加 `project_id`，仍允许工作区级任务不传参数。
- `tests/test_mcp_server_presentations.py`
  - 新增工具调用回归：旧实现对项目任务缺少范围的确认返回 `200`，修复后返回 `400`；跨项目同样拒绝，正确项目可确认；无效项目生成被拒绝。
- `core/compileall`
  - 新增路径完成 Python 编译检查。

## 验证

- 旧实现回归先失败：`/tools/call` 确认项目 PPT 任务不带 `project_id` 返回 `200`。
- 修复后定向：PPT 服务、HTTP、工具注册表共 `28 passed, 1 warning`。
- `python -m compileall -q core` 通过。
- 全量 `scripts/check.ps1`：后端 `1047 passed, 1 warning`；前端生产构建通过，7 组测试共 `46 passed`；Tauri `cargo check --locked` 通过。

## 边界

- `/tools/call` 仍是本机单用户受控入口，不是多用户授权系统；本轮只消除同一工作区内 HTTP 与工具路径的范围语义分叉。
- 工具 schema 的 `project_id` 不是安全授权凭据；真正的授权判断仍以持久化任务和项目目录为准。
- 这项修补不等于 PPT 事实、引用或 WPS 视觉质量通过；相关证据门和人工交付门继续单独验收。
