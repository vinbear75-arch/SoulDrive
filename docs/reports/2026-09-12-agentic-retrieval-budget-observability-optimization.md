# 有界 Agentic Retrieval 预算与实际尝试次数可观测性

日期：2026-09-12

## 目标

SoulDrive 当前采用的是有界 Agentic Retrieval：查询理解/改写、混合检索、GraphRAG 路由、一次纠错检索、证据门和有限回答校验重试。外部 Agent 框架普遍把循环上限、节点重试和终止状态作为显式运行约束；本轮将本地已经存在的硬上限从隐含常量提升为结构化审计字段，避免把“有一次重试”误读成自由 Agent 循环。

## 外部实践对照

- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)提供 recursion limit 和当前 step 计数，用于在达到上限前选择降级路径；[Fault tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance)将节点 `max_attempts`、超时和错误处理区分开。
- [LlamaIndex AgentWorkflow 的实现](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/agent/workflow/multi_agent_workflow.py)在达到 `max_iterations` 时明确停止或进入 early-stopping 分支。
- [open_deep_research 的配置](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/configuration.py)把 supervisor research iterations 和单步 react tool calls 分别限制；其[研究器循环](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/deep_researcher.py)在无工具调用、完成工具或达到上限时结束。

本地没有引入这些框架或依赖：项目仍保持 Windows 本机单用户、同步 SQLite 和已有 RAG 链路。借鉴点只落在“上限必须可见、实际次数必须可核对”。

## 本轮实现

`core/rag_engine.py`：

- 将检索变体上限命名为 `MAX_RETRIEVAL_VARIANTS = 3`，并统一用于反馈变体、工作流焦点和查询改写变体的最终边界；
- 将查询改写调用上限命名为 `MAX_QUERY_REWRITE_CALLS = 1`，记录是否实际发起过改写调用；
- `rag.retrieval_planned` 和 `agentic_retrieval_trace_v1` 增加 `agentic_budget`：最大检索轮数、最大检索变体数、最大查询改写次数、最大回答尝试次数和本次是否实际改写；
- `rag.completed`、无效回答拒答和质量 fallback 审计增加 `answer_attempts`/`max_answer_attempts`；
- 不改变原有分支次数，只把已有边界与实际执行次数写出来。

## 失败优先与回归

新增/扩展回归覆盖：

- 历史指代触发改写时，审计确认 `max_query_rewrite_calls=1`、`query_rewrite_attempted=true`、最终变体数量不超过 3；
- 纠错检索回调确认 `max_retrieval_passes=2` 和 `max_answer_attempts=2`；
- 回答第一次引用校验失败、第二次通过时，`rag.completed.answer_attempts=2`；
- 既有 GraphRAG 范围 fail-closed 用例仍保持通过。

验证结果：

- RAG/GraphRAG 定向：`119 passed`；
- 全量 `scripts/check.ps1`：后端 `1195 passed, 1 warning`；
- 前端生产构建通过，7 组测试合计 `49 passed`；
- Tauri `cargo check --locked` 通过。

## 结论与边界

现在可以从单条 trace 判断一次请求的检索/改写/回答预算与实际使用量，能够发现是否发生了超出设计上限的代码回归。该改动不证明 Recall、MRR、引用正确率、答案事实准确率、P50/P95 延迟或显存收益；当前仍不是带自由工具循环的 Agentic RAG，也不是 durable workflow checkpoint。真实 UI/E2E、安装包、模型服务中断恢复和跨存储 exactly-once 仍需独立验收。
