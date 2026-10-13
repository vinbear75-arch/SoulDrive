# SoulDrive 研究窗口约束与记忆管理时间状态优化

日期：2026-09-10

本轮继续遵循“外部实践对照 → 当前代码反例 → 先写失败回归 → 最小实现 → 定向/全量验证”。目标不是引入新的 RAG 或记忆框架，而是把已有的会话角色和时间记忆能力接到真实用户可见链路上。

## 外部实践对照

- [LangGraph StateGraph](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/graph/state.py) 将运行时上下文与可变状态区分；[LangGraph workflows/agents](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/workflows-agents.mdx)也区分预先定义的 workflow 与动态决定下一步的 agent。对应到 SoulDrive：研究窗口的角色约束属于服务端绑定的运行时契约，不应拼进用户检索词，也不应允许请求临时覆盖。
- [Open WebUI Knowledge](https://github.com/open-webui/docs/blob/main/docs/features/workspace/knowledge.mdx) 把知识查询、精确查看和处理状态分开。对应到 SoulDrive：运行时记忆注入与管理列表不是同一个查询用途；管理端需要保留可编辑记录并展示其状态，不能直接复用运行时过滤后列表。
- [Graphiti temporal memory](https://github.com/getzep/graphiti/blob/main/mcp_server/src/graphiti_mcp_server.py) 使用 `valid_at`/`invalid_at` 和 `reference_time` 表达事实时间范围；[LangGraph Store](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/stores.mdx)强调显式 namespace 与查询范围；[Letta memory schema](https://github.com/letta-ai/letta/blob/main/letta/schemas/memory.py)将核心记忆与归档记忆区分。对应到 SoulDrive：`valid_from`/`invalid_at` 继续由底层行和 `as_of` 查询裁决，项目/全局范围继续由服务端决定，不把管理列表误认为上下文注入结果。

## 发现的实际缺口

### 研究窗口

`WORKFLOW_CATALOG` 已经为 `paper_review`、`experiment_compare` 和 `meeting_actions` 定义了 `session_instruction`，但 `/chat` 为避免污染 RAG query 后，回答提示词只保留了证据/输出契约，窗口说明没有真正进入模型上下文。结果是窗口角色虽然持久化，差异只部分体现在输出契约，不能完整约束回答方式。

### 记忆管理

运行时检索已经支持当前时间和历史 `as_of`，但管理列表同时承担查看、编辑、停用和删除活动 head 的职责。把管理列表直接改成 `context_only=True` 会隐藏计划/过期 head，甚至返回其历史版本；用户看到的版本可能无法用当前 `updated_at` 编辑。另一方面，原 UI 把所有活动 head 都写成“已启用偏好”，没有告诉用户一条事实是当前生效、计划生效还是已过期。

## 实现

### 会话约束进入系统提示

`core/rag_engine.py::build_answer_messages` 现在在 workflow contract 区域加入服务端绑定的 `session_instruction`：

- 约束进入系统级提示词，不作为文档证据；
- 用户原始问题继续单独作为 RAG query；
- 用户材料不能覆盖服务端窗口契约；
- 没有 `session_instruction` 的通用会话保持原行为。

这样三个研究窗口的区别同时落在持久化 `workflow_id`、服务端提示约束和输出/引用契约上，而不是只改变 UI 选项。

### 记忆管理显示时间状态

`core/mcp_server.py` 增加服务端时间状态计算：

- `active_now`：当前时间位于有效窗口内；
- `scheduled`：`valid_from` 尚未到达；
- `expired`：`invalid_at` 已到达。

管理 API 的范围规则为：

- 没有 `project_id` 时仍只列全局记忆；
- 显式项目时保留该项目和允许的全局活动 head，包括计划/过期记录，以便编辑、停用和删除；
- 每条记录返回 `validity_status`，更新接口返回的 memory 也带同一状态；
- 运行时 `_learning_context_for_query` 和 `/learning/search` 不变，仍按 `context_only`、项目策略和显式 `as_of` 过滤，不会把计划/过期记录注入回答。

`souldrive-ui/src/App.tsx` 在偏好画像卡片显示“当前生效”“计划生效”“已过期”，不再把三类记录混成同一个“已启用”状态。

## 验证证据

- 先写失败回归：管理列表时间状态用例在旧行为下无法区分三类记录；修复后通过。
- 记忆与 API 组合：`tests/test_learning_store.py tests/test_mcp_server_learning.py`，`111 passed, 1 warning`。
- 研究窗口与提示词组合：`tests/test_rag_prompting.py tests/test_multimodal_workflows.py`，包含 `session_instruction` 回归；与相关 GraphRAG/TaskJournal/PPT 定向组合共 `166 passed, 1 warning`。
- 前端 `npm run test`：7 组共 `48 passed`；`npm run build` 成功。
- 仓库总检查：本轮后续加入评测上下文精确率后的 `scripts/check.ps1` 后端 `1083 passed, 1 warning`，Python compileall 通过，前端构建/测试通过，Tauri `cargo check --locked` 通过。

## 明确未做的事情

- 没有引入 LangGraph、Graphiti、Letta、Mem0 或向量数据库依赖；这些项目只提供机制对照。
- 没有把计划记忆自动调度成后台任务，也没有把所有相似记忆自动合并或自动失效；冲突失效仍遵循已有人工确认和受限 supersession 规则。
- 没有据此宣称三个研究窗口的答案质量已经不同或提升；这仍需冻结同一评测集，对比窗口契约、证据覆盖、引用正确性、拒答率和延迟。
- 没有关闭真实 Tauri/WPS、真实模型/视觉/语音、NSIS/MSI 安装包和跨存储 exactly-once 的人工/发行边界。
