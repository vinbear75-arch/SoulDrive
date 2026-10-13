# 草稿与产物项目范围优化记录

日期：2026-09-09  
范围：邮件草稿的读取、确认、发送和通用草稿详情路由；工作区级日程语义保持不变

## 结论

本轮发现并修复一个真实的项目边界缺口：邮件草稿创建时已经写入 `project_id`，但之后的 `/drafts/{artifact_id}`、`/status` 和 `/send` 路由只按全局唯一 `artifact_id` 查找。只知道 ID 的调用方可以读取草稿，或者在没有明确项目范围时尝试改变状态/发送。

现在项目绑定的产物必须在每次读取或变更时携带完全匹配的 `project_id`：缺少范围、范围错误和跨项目范围均返回 `409`；工作区级日程仍允许不带项目参数访问，避免把现有“我的日程是工作区级日历”的产品语义误改成项目级。

## 外部实践对照

- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)要求通过显式 `thread_id` 读取持久化线程状态，并将长期记忆放入明确的 namespace；不能只凭一个可能全局唯一的对象 ID 猜测上下文。
- [Open WebUI memories router](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/memories.py)在路由层按当前用户读取和变更记忆；其近期[安全公告](https://github.com/open-webui/open-webui/security/advisories/GHSA-hmjq-crxp-7rjw)说明，仅知道 ID 时返回他人记忆内容也是访问控制缺陷。
- [Open WebUI 数据库设计](https://github.com/open-webui/docs/blob/main/docs/reference/database-schema.md)把资源授权单独建模为 access grant，而不是把“拿到资源 ID”当作授权本身。SoulDrive 是单机单用户项目，不照搬 RBAC，而是采用更小的显式项目范围门。

## 实现

- `core/mcp_server.py`
  - 新增 `_artifact_project_scope_response`，统一比较存储的 `artifact.project_id` 与请求查询参数。
  - `GET /drafts/{artifact_id}`、`POST /drafts/{artifact_id}/status`、`POST /drafts/{artifact_id}/send` 增加 `project_id` 查询参数并执行范围校验。
  - PATCH/DELETE/ICS 路由也复用同一门；当前工作区级日程不传参数仍可用，未来项目绑定产物不会自动退化为全局访问。
- `souldrive-ui/src/App.tsx`
  - 邮件草稿确认和发送从返回的产物中读取 `project_id`，把同一范围显式带回 API。
  - `DraftArtifact.project_id` 改为允许工作区级日程的 `null`。
- `tests/test_topic5_drafts.py`
  - 新增跨项目/缺失项目范围的详情、状态变更和发送回归；发送越界时 fake 邮件发送器保持未调用。
  - 更新既有邮件流程，确认和发送均使用正确项目范围；日程测试继续验证工作区级语义。
- `souldrive-ui/scripts/learning-review.test.mjs`
  - 增加前端静态回归，确认确认/发送请求拼接 `project_id`。

## 验证

- 回归先在旧实现失败：项目邮件草稿不带 `project_id` 读取返回 `200`，目标为 `409`。
- 修复后：`tests/test_topic5_drafts.py` 为 `13 passed, 1 warning`。
- 前端：`npm run test:learning` 为 `3 passed`；`npm run test:schedule` 为 `4 passed`；生产构建通过，仅保留既有 Vite 单 chunk 超过 500 kB 提示。
- 仓库级 `scripts/check.ps1`：后端 `1045 passed, 1 warning`；前端生产构建通过，7 组测试合计 `45 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是多用户 RBAC，也不把单机应用扩展成云端租户系统；它只保证已存在的项目字段不会在后续产物操作中被全局 ID 绕过。
- `/schedules` 与 `/schedules/history` 仍是明确的工作区级日程列表；如果未来要按项目显示日程，应先改变数据模型/产品语义并新增评测，不通过本轮查询参数暗改。
- 通过范围门不代表邮件真实外发成功；真实 SMTP 发送仍需单独确认，自动化只使用本地 fake/sink。
