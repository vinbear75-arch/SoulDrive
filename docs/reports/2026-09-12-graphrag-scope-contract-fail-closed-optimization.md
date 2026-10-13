# GraphRAG 范围契约缺失时的 fail-closed 优化

日期：2026-09-12

## 结论

本轮不是增加新的 GraphRAG 或 Agentic RAG 框架，而是修复一个可复现的项目边界旁路：当图检索后端或 GraphRAG 原始证据回读器不支持 `project_id`/`global_only` 参数时，旧兼容代码会退回无范围调用。这样会让无项目请求重新读取混合默认图索引，或让全局共享原始证据回读器带回项目资料。

现在的行为是：

- 无项目、非显式跨项目问题：没有范围能力的 GraphRAG 后端直接返回空图结果，不再调用混合默认索引；
- `global_only=True` 的原始证据回读：回读器没有范围参数时直接拒绝该回读，不再降级为无范围 `chunk_id` 查询；
- 显式跨项目/全局问题仍允许使用明确授权的无范围兼容后端；
- 审计观测增加 `reason=graph_scope_contract_unavailable` 与 `source_scope_contract_unavailable`，能区分“没有证据”和“后端无法证明范围”。

普通混合检索、项目 GraphRAG、项目+全局共享 GraphRAG 仍走原有安全路径。

## 外部实践对照

- [Qdrant Filtering](https://qdrant.tech/documentation/search/filtering/) 把业务过滤条件作为搜索/读取条件，而不是在结果返回后才补救；[Qdrant 多分区实践](https://qdrant.tech/documentation/tutorials/multiple-partitions/)明确要求用租户字段过滤以隔离结果。
- [Weaviate Multi-tenancy operations](https://weaviate.io/developers/weaviate/manage-data/multi-tenancy) 要求每次 Get/Aggregate 查询带租户，未提供租户不能假定查询天然隔离。
- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api) 与 [Fault tolerance](https://docs.langchain.com/oss/python/langgraph/fault-tolerance) 将循环步数、重试次数和节点边界显式化；[open_deep_research 的配置](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/configuration.py)也把研究迭代数和单步工具调用数作为硬上限。SoulDrive 当前继续使用本机、无新依赖的有界检索链路，不因这些项目存在就引入自由循环 Agent。

## 失败优先证据

新增两项失败测试后，旧实现均先失败：

1. `test_unbound_graphrag_backend_without_scope_contract_fails_closed`
   - 无项目请求传给不支持范围参数的旧图后端；旧代码退回 `retrieve(query, mode, limit)`，返回了混合项目社区摘要。
2. `test_unbound_source_loader_without_scope_contract_fails_closed`
   - 全局-only 图指针交给不支持 `global_only` 的旧证据回读器；旧代码去掉参数重试，返回了 `project-b` 的原始证据。

## 最小实现

- `core/rag_engine.py`
  - 图后端缺少 `project_id` 能力时，仅对显式跨项目问题保留无范围兼容；普通无项目请求和项目请求 fail-closed；
  - 原始证据加载在 `global_only=True` 且后端缺少 `global_only` 能力时拒绝回读；项目+全局请求不会把无法证明范围的共享分支混入结果；
  - 保留结构化观测，不把兼容失败伪装成“没有命中”。
- `tests/test_graph_rag.py`
  - 更新旧兼容假件使其显式声明范围参数；
  - 新增两个不安全旧接口的负向回归。

## 验证

- GraphRAG 专项：`30 passed`；
- RAG/GraphRAG/范围/安全组合：`186 passed`；
- 全量 `scripts/check.ps1`：后端 `1195 passed, 1 warning`；
- 前端生产构建通过，7 组前端测试合计 `49 passed`；
- Tauri `cargo check --locked` 通过。

唯一 warning 仍为测试环境中的 `starlette.testclient` 与 `httpx` 兼容弃用提示，不是本轮失败。

## 边界

本轮证明的是范围契约缺失时不会通过已知兼容旁路泄露 GraphRAG 结果；没有证明社区摘要质量、召回率、引用正确率或答案事实准确率提升，也没有证明所有第三方/用户自定义后端都诚实执行其已声明的 `project_id` 过滤。当前链路仍是有界 Agentic Retrieval，不是自由多工具 Agentic RAG；真实 Tauri/WPS/UI/E2E、安装包启动和跨存储 exactly-once 仍需独立验收。
