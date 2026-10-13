# SoulDrive GraphRAG 评测范围优化

日期：2026-09-09

## 外部实践对照

- [Microsoft GraphRAG 查询总览](https://microsoft.github.io/graphrag/query/overview/)把 Local Search、Global Search 和 DRIFT Search 作为不同查询路径；其中 Local Search 将知识图谱信息与原始文本块共同用于回答，Global Search 则针对全数据集社区报告做 map-reduce。
- [Microsoft GraphRAG Global Search 说明](https://github.com/microsoft/graphrag/blob/main/docs/query/global_search.md)强调，全局问题不能只用普通向量相似度代表；查询评测必须知道自己实际执行的是哪种 GraphRAG 路径。
- [Haystack MultiRetriever 文档](https://docs.haystack.deepset.ai/docs/multiretriever)展示了多路检索并行后用 RRF/去重合并的做法。SoulDrive 已有关键词、向量和图候选合并，但这不等于每次评测都真的启用了图路。

这些项目没有授权 SoulDrive 直接引入新依赖。本轮只迁移可验证的工程原则：评测样本声明为图谱相关时，必须把实际图存储传入候选召回；没有图谱相关标记的样本不额外打开图数据库。

## 发现的本地缺口

`scripts/eval_rag.py:run_retrieval` 原来只调用：

```text
kb.search_with_evidence(question, top_k=..., project_id=...)
```

没有传入 `graph_db`。但 `summarize` 仍会按 `graph_relevant=true` 的样本计算 `graph_context_hit_rate`。因此旧指标可能只是关键词/向量混合检索命中，却被报告成“图谱相关题命中率”，评测结论不具备可追溯性。

## 最小修复

- 新增 `_open_retrieval_graph_store(workspace)`：有解锁密钥时使用与答案生成相同的 `SecureGraphStore`，否则使用本地 `LocalGraphDB`。
- `run_retrieval` 仅当冻结样本中存在 `graph_relevant=true` 时打开一次图存储，并在对应样本调用 `search_with_evidence(..., graph_db=graph_db)`。
- 保持普通样本的调用形态不变，避免无关评测因为图数据库初始化或图查询产生额外延迟。
- 评测结束后显式关闭图存储，避免 CLI 多轮评测留下 SQLite 连接。

本轮没有新增 Microsoft GraphRAG 的社区报告 map-reduce、DRIFT、多代理查询或新的重量级依赖。当前小型本机项目尚未有同一冻结集上的全局问题基线，先保证现有 `graph_context_hit_rate` 不虚标；是否增加 Global/DRIFT 路由应在独立数据集、延迟和答案质量基线后再决定。

## 失败优先与验收证据

- 先新增反例测试 `test_live_retrieval_enables_graph_store_for_graph_relevant_records`；修复前因评测模块没有 `_open_retrieval_graph_store` 而失败，证明测试不是只验证旧行为。
- 修复后 `tests/test_eval_assets.py`：`36 passed`。
- GraphRAG、加密图存储和项目范围检索回归：`tests/test_graph_rag.py tests/test_secure_graph_store.py tests/test_secure_retrieval_flow.py`，`47 passed`。
- 随后执行仓库级 `scripts/check.ps1`：后端 `1023 passed, 1 warning`；前端生产构建通过，7 组测试共 `44 passed`；Tauri `cargo check --locked` 通过。

## 仍然没有宣称的内容

- `graph_context_hit_rate` 现在能证明图路被启用，但它仍是“期望来源命中”指标，不是 GraphRAG 答案正确率，也不是社区报告质量指标。
- 当前没有实现 Global Search、DRIFT Search、Agentic RAG 或 LLM-as-a-judge；这些需要独立的冻结数据、路由标签、成本/延迟和答案质量基线。
- 真实工作区的图索引完整性、真实模型生成质量、Tauri/UI/E2E、安装包和跨进程崩溃恢复边界仍需分别验证。

## 2026-09-09 后续闭环：关系召回与路由观测

继续对照 Microsoft GraphRAG 的 Local Search 设计后，发现本地社区检索的打分文本此前遗漏了已持久化的 `relationships` 字段。新增关系词反例：查询“依赖”会在修复前退化到关系无关的 `C001` 社区；修复后 `C002` 的关系文本被正确命中，并回读对应原始 chunk。

同时收紧评测与运行观测：

- `LocalKnowledgeBase` 的 `retrieval_observability_v1` 现在记录 `graph_route`、`graph_recall_enabled` 和 `graph_context_count`。
- `scripts/eval_rag.py` 保存并读取这份观测，新增“图路由选择率、图阶段执行率、图上下文非空率”；历史 `graph_context_hit_rate` 保留为兼容字段，但明确它只是预期来源命中，不再把它当成 GraphRAG 成效。
- `RAGEngine` 的图路由改为基于最终稳定的 contextual retrieval query，避免“原句无关系词、选区上下文有关系词”时底层检索器和 GraphRAG 走不同路径。
- `on_retrieval_completed` 延后到一次有界纠错检索确定最终证据后，并返回最终 retrieval mode、图路由和纠错标志，避免 UI/评测记录第一次中间路径。

本轮定向验证：`tests/test_graph_rag.py tests/test_eval_assets.py tests/test_rag_prompting.py tests/test_secure_retrieval_flow.py` 组合 `137 passed`；随后包含后续记忆 outbox 回放、指数退避、claim fencing、会话角色契约和幂等派生投影范围回归的仓库全量后端为 `1043 passed, 1 warning`，前端构建与测试 `44 passed`，Tauri `cargo check --locked` 通过。

仍未宣称 GraphRAG Global/DRIFT 或 Agentic RAG 已达标：当前新增的是可追溯的本地关系召回和路由证据，不是社区摘要质量、答案正确率或多轮工具循环的证明。
