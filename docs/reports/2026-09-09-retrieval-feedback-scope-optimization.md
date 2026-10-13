# 检索反馈项目范围优化记录

日期：2026-09-09  
范围：检索改写反馈的保存、召回和撤销

## 结论

本轮发现并修复一个真实的项目范围缺口：检索反馈已经在保存和召回时区分全局与项目范围，但撤销接口只接受全局唯一 `feedback_id`，没有验证项目范围。只知道 ID 的调用方可以在不提供项目上下文，或提供另一个项目上下文时，撤销项目级检索改写。

现在项目级反馈撤销必须携带完全匹配的 `project_id`；缺少范围或跨项目范围返回 `409`。全局反馈仍只能在不带 `project_id` 的工作区语义下撤销；带项目参数不会把全局资源伪装成项目资源。前端撤销按钮使用反馈记录自身的持久化范围，不从当前界面猜测。

## 外部实践对照

- [Open WebUI knowledge router](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/knowledge.py)在列出资源时先按用户/组过滤，再批量计算写权限；资源 ID 不直接等于可写权限。
- [Open WebUI knowledge filesystem](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/tools/knowledge_fs.py)在文件解析前重新校验知识库访问权，并限制模型绑定的知识库集合；后台工具不会因为拿到一个 ID 就跳过范围判断。
- [Open WebUI 数据库设计](https://github.com/open-webui/docs/blob/main/docs/reference/database-schema.md)将 resource、principal 和 permission 分开存储。SoulDrive 是本机单用户产品，不引入多用户授权表，而是用已有的 `scope/project_id` 做同等的资源变更边界。
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)要求以明确的 `thread_id` 读取持久状态；这里对应到检索反馈就是“反馈 ID + 持久化范围”共同构成可变更上下文。

## 实现

- `core/learning_store.py`
  - `revoke_retrieval_feedback` 接受可选 `project_id`。
  - 全局反馈带项目参数、项目反馈缺少参数、项目参数不匹配均拒绝；已撤销或不存在的记录仍保持原有的空结果语义。
- `core/mcp_server.py`
  - 撤销路由增加 `project_id` 查询参数。
  - 先验证请求项目存在，再把范围传入事实存储；范围冲突返回 `409`，不执行更新。
- `souldrive-ui/src/App.tsx`
  - 撤销函数改为接收完整反馈记录，从 `feedback.project_id` 拼接范围参数。
- `tests/test_mcp_server_learning.py`、`tests/test_learning_store.py`
  - 新增旧实现失败回归：项目反馈无范围撤销原返回 `200`，目标为 `409`。
  - 覆盖缺少范围、错误项目和正确项目三种 API 行为，并验证存储层也拒绝不匹配范围。
- `souldrive-ui/scripts/learning-review.test.mjs`
  - 增加前端静态回归，确认撤销请求携带反馈自身范围。

## 验证

- 旧实现：项目反馈不带 `project_id` 撤销返回 `200`，回归先失败。
- 修复后：学习存储与 API 定向回归 `47 passed, 1 warning`；范围专项额外 `2 passed, 1 warning`。
- 前端 `npm run test:learning`：`3 passed`。
- 全量 `scripts/check.ps1`：后端 `1046 passed, 1 warning`；前端生产构建通过，7 组测试共 `46 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这不是多用户 RBAC；它只防止已存在的项目范围在变更入口被全局 ID 绕过。
- 撤销反馈不会修改原文档、向量索引或 GraphRAG 图；它只使后续查询不再使用该条用户确认的改写。
- 这是一个范围修补，不是检索质量提升证明；反馈是否改善答案仍需冻结评测集逐样本验证。
