# SoulDrive 检索诊断与会话窗口链路对齐优化

日期：2026-09-15

## 外部实践对照

- [Microsoft GraphRAG local search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md) 将查询范围、检索上下文和原始来源回指作为同一条查询链路的一部分；诊断结果如果没有复现实际查询条件，就不能代表运行时行为。
- [LlamaIndex vector-store metadata filters](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/vector_stores/types.py) 将过滤条件作为检索接口的显式参数，而不是依赖调用方事后猜测。SoulDrive 的业务窗口重点同样应成为显式、可审计的检索输入。

## 发现的缺口

生产 `/chat` 已根据 `paper_review`、`experiment_compare`、`meeting_actions` 生成受限的 workflow 检索重点；只读 `/diagnostics/retrieval` 之前没有 `workflow_id`，因此论文精读、实验对比和会议纪要的诊断结果与实际聊天链路不一致，无法用于验证“不同会话窗口确实有不同作用”。

## 失败优先与最小实现

新增 `McpServerIndexingTests.test_retrieval_diagnostics_applies_bound_workflow_query_focus`，修复前先得到 `TypeError: _run_retrieval_test() got an unexpected keyword argument 'workflow_id'`。

当前实现：

1. `RetrievalTestRequest` 接受与聊天接口相同的三个白名单 workflow；未知窗口仍由请求模型拒绝。
2. 诊断执行合并最多 3 条已确认检索反馈和最多 2 条 workflow 检索重点，去重后最多传 5 条 `extra_query_variants`，不改变原始用户问题。
3. 返回 `workflow_id` 与 `workflow_retrieval_variant_count`，审计事件也记录窗口；旧知识库适配器不接受附加变体时只重试基础查询，并返回实际应用数 `0`。

## 回归证据

- `tests/test_mcp_server_indexing.py`：`22 passed, 1 warning`。
- 新增 legacy 适配器回归：旧签名不会 500，且不会谎报已应用 workflow 重点。

后续收尾复核：完整工程门禁后端 `1550 passed, 1 warning`，前端生产构建成功、UI 脚本 `62 passed`，`compileall` 与锁定 Cargo 检查通过；冻结 GraphRAG 路由 `4/4` 通过，研究窗口 runtime `5/5` 通过。

## 边界

这项改动只保证诊断请求与生产窗口检索计划对齐，不等于召回率、答案事实准确率或最终引用质量提升；旧适配器明确只能运行基础查询。后续仍需在冻结数据集上分别评估三个窗口的 Recall@K、来源隔离、延迟和答案契约。
