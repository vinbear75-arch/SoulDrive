# 研究会话角色输入门优化记录

日期：2026-09-09  
范围：`paper_review`、`experiment_compare`、`meeting_actions` 会话的纯文本入口、聊天 API 与前端提交链路

## 结论

本轮确认并修复了一个真实的角色边界缺口：媒体发送路径会调用 `/multimodal/workflows/plan`，但纯文本发送路径此前直接进入 `/chat`；API 也没有在 RAG 前复用同一套最低输入契约。因此 `experiment_compare` 或 `paper_review` 会话在缺少实验材料/论文文档时仍能进入普通 RAG，角色窗口只改变提示词，不能可靠改变执行边界。

现在三层规则一致：

1. 前端纯文本和媒体发送都先执行业务预检。
2. `/chat` 对直接 API、旧客户端和绕过 UI 的调用再次执行无副作用的输入门。
3. 输入不足或项目 Playbook 未审核时，RAG 不启动，返回 `422` 与可展示的 `workflow_plan.missing_inputs`；已完成的幂等请求仍优先重放，不会因当前输入状态变化破坏耐久语义。

## 外部实践对齐

- LangGraph 的公开实现把线程状态、可恢复中断和人工审核建立在持久化 checkpoint 之上；工作流应在进入后续节点前保留并校验状态，而不是只把角色名称拼进提示词。参考：[LangGraph Persistence](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/persistence.mdx)、[LangGraph interrupt](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py)。
- Haystack 的 pipeline 在运行前校验输入与管线状态，缺少必需输入时不继续执行组件。SoulDrive 本轮采用同样的轻量原则，但复用已有确定性 `plan_workflow`，没有引入工作流框架。参考：[Haystack Pipeline.run](https://github.com/deepset-ai/haystack/blob/main/haystack/core/pipeline/pipeline.py)。
- [Qdrant Filtering](https://qdrant.tech/documentation/search/filtering/)和[多租户分区](https://qdrant.tech/documentation/tutorials/multiple-partitions/)把租户字段作为每次检索的显式过滤条件；[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)要求读取和恢复状态时显式提供 `thread_id`。SoulDrive 将这一原则映射到会话历史：项目会话读取必须携带 `project_id`，全局会话则必须保持无项目范围。

## 实现变更

- `core/mcp_server.py`
  - 新增 `_chat_workflow_input_plan`，从当前会话请求提取项目、文档、选区、视觉事件、实验运行和会议文本。
  - `/chat` 在 RAG、请求收据认领和模型执行前复用 `plan_workflow`。
  - 绑定 `paper_review` 时至少需要当前项目的一份已索引文档；绑定 `experiment_compare` 时至少需要两份材料、授权的视觉/选区证据或一对可比运行；`meeting_actions` 的直接文本继续作为用户记录证据，而不是伪装成 ASR。
  - 复用既有 Playbook 审核门；拒绝事件写入审计，`side_effect=none`。
  - 会话角色现在是不可临时覆盖的持久化契约：已绑定专用 workflow 的会话拒绝其他角色，通用会话也拒绝在请求中临时注入专用 workflow，必须新建或选择对应会话。
- `souldrive-ui/src/App.tsx`
  - 纯文本 `handleSubmit` 先调用 `ensureWorkflowReady`，与媒体路径保持同一业务门。
  - 项目会话历史请求显式携带当前 `activeProjectId`；服务端不再允许通过只给 session ID 读取项目会话历史。
- `tests/test_multimodal_workflows.py`
  - 增加“纯文本实验对比缺输入时不得进入 RAG”的回归。
  - 更新会话项目解析测试以提供明确文档范围。
  - 将旧的绕过契约改为验证输入门和 Playbook 审核门。
- `souldrive-ui/scripts/document-scope.test.mjs`
  - 增加前端静态回归，确认纯文本提交确实调用业务预检。

## 验证证据

- 定向业务模块及相关链路：`168 passed, 1 warning`。
- `tests/test_multimodal_workflows.py`：本轮原始角色输入门回归为 `22 passed, 1 warning`；后续加入服务端文档索引/项目范围复核后，该文件的专项结果已纳入 `tests/test_multimodal_workflows.py tests/test_secure_retrieval_flow.py tests/test_graph_rag.py` 的 `65 passed`。
- 前端工作流静态回归：`11 passed`。
- 前端生产构建：通过；仅保留已有的 Vite 单 chunk 超过 500 kB 提示。
- 新回归先在旧代码上失败（`200 != 422`），实现后通过；测试中的 Fake RAG 若被错误调用会主动抛错。
- 会话角色定向回归：通用会话临时覆盖为 `paper_review`、专用会话切换角色和研究窗口错误选择共 `3 passed, 1 warning`。
- 会话历史范围回归：全局会话无项目参数可读；项目会话缺少项目参数、项目参数错误或把项目参数用于全局会话均返回 `409`；正确项目范围返回 `200`。项目端点专项 `16 passed, 1 warning`。
- 前端静态回归新增“项目历史请求携带 activeProjectId”，文档/范围组共 `12 passed`。
- 随后仓库级 `scripts/check.ps1`：后端 `1045 passed, 1 warning`；前端生产构建与 7 组测试合计 `45 passed`；Tauri `cargo check --locked` 通过。

## 边界与未完成项

- 这证明了输入契约和执行前置门，不证明真实本地模型一定生成事实正确答案。
- 本报告发布后发现客户端可伪造“已索引”文档声明的缺口，已由 `docs/reports/2026-09-09-workflow-document-scope-validation-optimization.md` 补上：服务端现在逐份核验本地索引状态、全局共享范围和当前项目归属，未索引或跨项目文档在进入 RAG 前返回 `422`。
- 尚未把三种角色扩展成重量级 Agent 图；当前小项目继续采用确定性预检、项目过滤、证据命名空间和输出契约，避免没有评测收益的自由循环。
- 全量后端、Tauri、真实 UI/WPS 和打包启动仍需按执行证据中的边界单独解释，不能由本轮定向测试替代。
