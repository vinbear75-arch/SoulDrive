# GraphRAG 稳定文档身份与来源计数优化

## 结论

本轮修复了 GraphRAG 社区诊断把“展示文件名”当作“来源身份”的缺口。图数据库本来已经以 `document_id` 区分同名资产，但图快照、社区 payload 和诊断没有透传该身份；旧诊断只能按 `source_filenames` 计数，无法表达同名来源是否为同一份文档。

现在图快照为本地图数据库和加密图数据库统一暴露稳定 `document_id`；GraphRAG 社区索引写入 `source_document_ids`，社区 evidence 同步携带该字段，诊断的 `source_count` 优先按稳定文档身份计数。旧 payload 没有该字段时仍回退到文件名，避免历史索引直接失效；新索引 schema 标记为 `souldrive_graphrag_index_v4`，后续源快照变化会要求重建。

## 外部实践与本地映射

- [Microsoft GraphRAG BYOG 输入约定](https://github.com/microsoft/graphrag/blob/main/docs/index/byog.md)要求图摘要保留 `text_unit_ids`；本地对应保留 chunk 指针，并补齐其所属稳定文档身份。
- [Microsoft GraphRAG 关于社区报告回指原文的讨论](https://github.com/microsoft/graphrag/discussions/931)明确社区摘要是聚合结果，不能把社区报告冒充为精确原文引用；本轮只增加来源覆盖身份，不宣称报告能定位到单一原文句子。
- [LlamaIndex 的节点元数据实现](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/vector_stores/utils.py)把 `document_id/ref_doc_id` 作为独立字段保存；[Haystack 的 source ID 元数据](https://github.com/deepset-ai/haystack/blob/main/haystack/components/retrievers/sentence_window_retriever.py)也要求来源 ID 与 split ID 分开处理。SoulDrive 采用同样的“显示名与身份分离”原则。

## 失败优先与修复

- 新增同名来源诊断反例：两个稳定文档 ID 都显示为 `report.pdf` 时，旧逻辑会得到 `source_count=1`；修复后的诊断得到 `source_count=2`，并返回两个 `source_document_ids`。
- `LocalGraphDB.get_graph_snapshot()` 与 `SecureGraphStore.get_graph_snapshot()` 现在都透传数据库中的 `document_id`；缺失 ID 的兼容快照按项目范围和文件名生成确定性身份，仅作为迁移期间的有界回退。
- 社区 payload、层级主题、根节点和 GraphRAG 派生 evidence 保留稳定文档身份；原始 `source_filenames` 仍用于用户可读显示。
- 旧社区 payload 仍能诊断：没有 `source_document_ids` 时，`source_count` 回退到旧文件名数组，不把旧索引误报为已验证的新身份数据。

## 已验证

- GraphRAG、LocalGraphDB、SecureGraphStore 定向回归：`62 passed`。
- `git diff --check` 通过；定向结果不单独替代完整工程门，后续完整门已纳入前序 durable task 变更并记录如下。
- 随后在比较查询分解改动前，完整工程门已通过后端 `1440 passed, 1 warning`、前端 8 组脚本 `62 passed`、锁定 Cargo；查询分解改动后的最新完整工程门为后端 `1441 passed, 1 warning`，前端 `62 passed`，Cargo 仍通过。

## 边界

- 该改动证明来源身份和诊断计数不再依赖显示文件名，不证明社区摘要的语义质量、实体消歧、图谱召回率或最终答案准确率。
- `source_document_ids` 表示社区覆盖的文档身份集合，不是聚合报告的精确句子级引用；事实性回答仍必须回到原始 chunk evidence。
- 未引入 Microsoft GraphRAG、LlamaIndex、Haystack、Neo4j 或远程图服务；本机 SQLite 图存储和既有项目范围门保持不变。
- 不等于多跳 Agentic RAG、DRIFT、跨存储原子提交、外部 exactly-once 或真实 Tauri/WPS UI/E2E 已验收。
