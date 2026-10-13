# 研究窗口记忆搜索命名空间 API 优化

日期：2026-09-13

## 发现

聊天运行时已经把 `workflow_id` 传给 LearningStore，所以 `paper_review`、`experiment_compare` 和 `meeting_actions` 的上下文记忆能够按窗口隔离；但 `/learning/search` 这个用于 UI/诊断/重放的入口没有接收该字段。它会把 `context_only=True` 与缺失工作流绑定一起传入，导致合法的 `workflow_fact` 在该入口永远查不到，无法用同一入口验证三类研究窗口的差异。

## 公开实践对照

- [LangGraph/LangMem 的 namespace 与 metadata filtering](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/) 把长期记忆检索限定在显式 namespace，并支持按 metadata 过滤。
- [LangGraph 的 long-term store](https://langchain-ai.github.io/langgraphjs/how-tos/delete-messages/) 区分线程级短期状态和跨线程长期数据；本地把研究窗口当作既有长期记忆的显式 namespace，不复制另一套存储。
- [Letta 的 memory blocks](https://docs.letta.com/tutorials/attaching-detaching-blocks/) 强调切换任务时 attach/detach 可见上下文；本地继续由服务端 `workflow_id` 和既有读取门控制，不依赖前端自觉过滤。

## 实现

1. `LearningSearchRequest` 增加与聊天/反思接口一致的 `workflow_id` 白名单：只允许 `paper_review`、`experiment_compare`、`meeting_actions`。
2. `/learning/search` 将 `workflow_id` 传入 `LearningStore.search()`，因此项目范围、全局范围、时间点和工作流范围在诊断入口与聊天入口一致。
3. `learning.search_performed` 审计事件保存窗口 ID，仍只记录计数和范围元数据，不记录记忆正文。

## 失败优先与验收

- 新回归先复现旧行为：合法 `paper_review` 查询返回空列表；失败结果确认是请求模型未传递工作流 namespace。
- 实现后窗口搜索回放和未知窗口拒绝专项 `2 passed, 1 warning`；前一轮学习/MCP/记忆组合为 `168 passed, 1 warning`。
- 含该修复的完整工程门通过：后端 `1235 passed, 1 warning`；前端生产构建和 7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 这只是诊断/重放入口与运行时记忆范围的一致性修补，不代表已经完成情景记忆统一检索或答案质量评测。
- 未知 workflow 直接返回 422；缺失 workflow 仍按全局/项目的通用 context-only 语义运行，不会自动猜测窗口。
- 该入口仍不执行模型、GraphRAG 或 Agentic RAG 循环；工作流差异来自已冻结的服务端 namespace 契约。
