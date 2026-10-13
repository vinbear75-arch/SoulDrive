# RAG 父块扩展同分结果确定性排序优化

日期：2026-09-12

## 结论

启用来源多样性时，RAG 父块扩展最终重排现在在 `final_score`、`rerank_score` 和 `keyword_score` 并列时继续使用候选 `id` 作为稳定末级键。证据 Top-K 不再依赖父块扩展前的候选进入顺序。

## 外部实践对照

- [Microsoft GraphRAG Local Search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md) 将图实体、关系和原文 text units 分开候选、排序、过滤，再按固定上下文预算构建最终上下文；来源身份是上下文构建的组成部分。
- [GraphRAG local mixed context 实现](https://github.com/microsoft/graphrag/blob/main/packages/graphrag/graphrag/query/structured_search/local_search/mixed_context.py) 以实体访问顺序和关系数明确排序 text units，而不是依赖容器迭代顺序。
- [Haystack meta-field ranker](https://github.com/deepset-ai/haystack/blob/main/haystack/components/rankers/meta_field.py) 在排名阶段保留文档 ID 去重边界；[LlamaIndex node ID 说明](https://github.com/run-llama/llama_index/issues/9209) 区分原文 ID 与节点 ID，说明 chunk/node 身份必须贯穿检索后处理。

SoulDrive 没有引入 GraphRAG 的完整上下文框架；本轮只收紧现有来源多样性父块扩展的排序键。

## 失败优先验证

新增 `tests/test_retrieval.py::RetrievalTests::test_parent_expansion_uses_stable_candidate_id_for_equal_scores`：以比较查询触发来源多样性，逆序候选 `alpha/beta` 的父块和三个分数全部相同。

- 修复前按输入顺序返回 `alpha, beta`，证明最终重排仍依赖上游顺序。
- 修复后按既有反向排序约定稳定返回 `beta, alpha`；父块去重、来源预留和 Top-K 数量不变。

## 回归证据

- 检索、知识库路径、GraphRAG 定向回归：`90 passed`。
- 全量 `scripts/check.ps1`：后端 `1164 passed, 1 warning`；前端生产构建与 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮只证明父块扩展的并列排序可重复，不证明来源多样性策略本身提高 Recall、MRR、引用正确率或答案准确率，也不证明 GraphRAG 的图上下文质量已通过冻结业务评测集。
