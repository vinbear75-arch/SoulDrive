# 无项目普通检索的全局范围隔离优化

日期：2026-09-12

## 目标

让“未绑定项目的普通会话”与 GraphRAG 使用同一条范围契约：默认只读明确的全局资料；只有用户明确提出跨项目/全库问题时，才允许跨项目检索。这样研究窗口、项目窗口和全库比较窗口的作用才不会只停留在 UI 文案层。

## 公开项目对照

- [Qdrant Filtering](https://qdrant.tech/documentation/search/filtering/) 将业务过滤条件作为向量检索条件，并建议为过滤字段建立 payload index；范围不是召回后才由生成模型猜测的标签。
- [Weaviate Search Basics](https://docs.weaviate.io/weaviate/search/basics) 要求启用多租户后每次查询指定 tenant；[Weaviate Filtering](https://docs.weaviate.io/weaviate/concepts/filtering) 说明过滤应在向量搜索前参与候选空间约束。
- [Microsoft GraphRAG Query Overview](https://microsoft.github.io/graphrag/query/overview/) 区分 local search 与面向全语料社区报告的 global search。SoulDrive 沿用这条“普通范围与显式全局范围分开”的原则，但保持当前 SQLite/本地索引实现，不引入外部向量库或多租户框架。

## 失败优先发现

原链路在 `LocalKnowledgeBase.search_with_evidence()` 中只有 `project_id` 存在时才把项目范围传入 dense、keyword、source、section 和图召回；当 `project_id=None` 且问题没有显式跨项目意图时，检索会把项目 A、项目 B 和全局文档一起放入候选集。

回归 `test_unbound_search_uses_only_global_documents_without_explicit_cross_project_scope` 先写入两个项目私有文档和一个全局文档，再执行无项目的普通问题。旧实现实际返回 `project-a.pdf` 与 `project-b.pdf`，与项目隔离要求冲突。

## 最小实现

新增单次检索的 `unbound_global_only` 判定：

```text
无有效 project_id 且 query_intent 不是 explicit_global_scope
    => global_only=True
```

该标志统一传给：

- dense 向量候选；
- keyword/FTS 候选；
- source title、source profile 和 section tree 候选；
- graph recall 与 graph context 的实体/关系读取；
- RAGEngine 根据 GraphRAG chunk provenance 回读原始证据的范围过滤。

显式“比较两个项目/跨项目/全库”等问题继续走 `global_explicit`，不被错误收窄。项目会话原有的“项目优先、全局补充/全局回退”也保持不变。

## 验证结果

- 失败优先回归先复现了两份项目私有来源混入无项目普通检索。
- 检索、项目回退、加密检索、GraphRAG、RAG 提示词组合：`179 passed`。
- 新增 GraphRAG 引擎证据回读回归，确认无项目图索引的 chunk 回读携带 `global_only=True`。
- 完整 `scripts/check.ps1` 已通过：后端 `1189 passed, 1 warning`；前端构建通过，7 组前端测试全部通过；Tauri `cargo check --locked` 通过。

## 边界

- 这是本机单用户项目空间的范围防御，不是企业多租户 RBAC，也不宣称解决外部向量库的访问控制。
- `global_only` 只按已入库元数据的空 `project_id` 判定全局；低质量或错误元数据仍应在摄取校验阶段拒绝或进入人工确认。
- 这项修复证明候选范围与证据回读范围一致，不证明召回率、答案准确率、GraphRAG 摘要质量或 Agentic RAG 的收益。
- 未新增 Qdrant、Weaviate 等依赖，避免为本机小项目引入未被评测证明的基础设施复杂度。
