# 关键词检索同分结果确定性排序优化

日期：2026-09-12

## 结论

SQLite FTS5 关键词索引现在按 `bm25 score ASC, doc_id ASC` 返回结果。同一查询、同一分数的候选不再依赖 FTS 内部或插入顺序；项目过滤、文档名过滤和加密向量库路径保持不变。

## 外部实践对照

- [Qdrant 同分混合检索 issue](https://github.com/qdrant/qdrant/issues/6814) 记录了 RRF 同分文档重复查询时顺序变化的问题，并提出用 UUID 作为 tie-breaker 保持一致排名。
- [LlamaIndex fusion retriever](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/retrievers/fusion_retriever.py) 以节点哈希作为融合身份，说明融合排序不能把列表位置当作文档身份。
- [Haystack meta-field ranker](https://github.com/deepset-ai/haystack/blob/main/haystack/components/rankers/meta_field.py) 在排名前按文档 ID 去重，保留文档身份与排序阶段的边界。

SoulDrive 没有引入新的向量数据库或融合框架；本轮只把同分的稳定身份键补进既有 SQLite FTS 查询。

## 失败优先验证

新增 `tests/test_keyword_index.py::KeywordIndexTests::test_keyword_index_uses_stable_doc_id_tiebreaker_for_equal_scores`：逆序写入 `doc-b`、`doc-a`，内容和 BM25 信号完全相同。

- 修复前返回 `doc-b, doc-a`，证明同分时依赖写入顺序。
- 修复后稳定返回 `doc-a, doc-b`；`SecureVectorStore.keyword_candidates()` 原有 `(-score, chunk_id)` 稳定排序也通过独立回归，没有重复改写。

## 回归证据

- 关键词索引、加密向量库、检索组合：`54 passed`。
- 全量 `scripts/check.ps1`：后端 `1163 passed, 1 warning`；前端生产构建与 7 组测试 `48 passed`；Tauri `cargo check --locked` 通过。

## 边界

本轮只证明关键词检索同分结果可重复，不证明 BM25/向量召回率、重排质量、答案事实正确率、跨项目过滤正确性已整体提升，也不把排序稳定性当作 RAG 质量提升。
