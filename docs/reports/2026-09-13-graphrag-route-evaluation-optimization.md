# GraphRAG 查询路由质量门优化

日期：2026-09-13

## 发现

SoulDrive 已经有确定性的 `route_graph_query()`：普通事实问题保持混合检索，关系/影响链/多跳问题走 local GraphRAG，跨文档/总体归纳走 global GraphRAG；关系信号优先级高于“整体”等宽泛词。但原有证据主要散落在单元测试和 live acceptance 脚本，尚未形成一份冻结的路由契约集。

## 公开实践对照

- [Microsoft GraphRAG Query Overview](https://github.com/microsoft/graphrag/blob/main/docs/query/overview.md) 将 Local、Global、DRIFT、Basic 和 question generation 作为不同查询方法；Local 适合实体局部问题，Global 适合跨语料总结。
- [Microsoft GraphRAG Local Search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md) 明确 local 路径把图结构与原文 chunk 一起筛选进上下文；本地继续保留原文证据门，不把社区报告当作独立事实来源。
- [LightRAG 查询模式](https://github.com/HKUDS/LightRAG/blob/main/lightrag_webui/src/api/lightrag.ts) 把 naive/local/global/hybrid/mix/bypass 作为显式 query mode；本地采用更窄的三态契约，避免为小型本机项目增加不必要的 bypass 或自由模式。
- [Haystack ConditionalRouter](https://docs.haystack.deepset.ai/docs/conditionalrouter) 要求路由条件、输出连接和可测试条件显式化；本地把 expected mode、是否启用图、命中信号写入冻结样本。

## 实现

1. 新增 `scripts/eval_graphrag_route.py`，对真实 `route_graph_query()` 执行确定性评测，不调用模型、不读取工作区、不把路由契约误写成答案准确率。
2. 新增 `eval/graphrag_route_quality_v0.1.jsonl`，冻结 4 个样本：普通事实 hybrid、关系/上下游 local、跨文档共同思路 global，以及“整体 + 影响链”时 local 优先的冲突场景。
3. 报告分别统计 `route_accuracy`、`graph_gate_accuracy` 和 `signal_contract_accuracy`，区分“模式选对”“是否启用图选对”“路由信号满足”三个层次。
4. 不引入 Microsoft DRIFT 或自由 Agentic RAG 循环；DRIFT/全局 map-reduce 的成本和外部模型依赖仍须在同数据、同延迟预算的留出评测中证明后才考虑。

## 失败优先与验收

- 新增回归第一次运行真实失败：`eval_graphrag_route` 尚不存在，pytest 收集阶段 `ImportError`。
- 实现后专项 `2 passed`；冻结路由数据集 `4` 个样本全部通过，`route_accuracy=1.0`、`graph_gate_accuracy=1.0`、`signal_contract_accuracy=1.0`。
- 完整 `scripts/check.ps1` 已通过：后端 `1226 passed, 1 warning`；前端生产构建通过，7 组脚本测试合计 `49 passed`；Tauri `cargo check --locked` 通过。

## 边界

- 该质量门只验证查询路由，不证明 GraphRAG 的实体抽取、社区划分、图召回证据质量或最终答案事实正确率。
- 4 个样本是最小路由契约，不是业务留出集；三类研究窗口仍需分别冻结真实任务集来比较检索质量和输出契约。
- 当前三态路由与本机规模和延迟预算匹配；不因“别人有 DRIFT/Agentic RAG”就自动引入新的循环、索引或模型成本。
