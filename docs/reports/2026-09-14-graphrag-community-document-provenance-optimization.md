# GraphRAG 社区文档摘要来源保真优化

## 结论

本轮修复了社区构建中的同名文档串入问题。GraphRAG 社区已经按实体关联的 `chunk_ids` 计算来源，但随后又按 `source_filename` 回收 `document_summaries`；两个项目都有 `report.pdf` 时，只命中项目 A chunk 的社区也会把项目 B 摘要挂进来。

现在社区先按 chunk provenance 选择相关文档，再生成可读的 `source_filenames`；同名文件不会扩大社区的文档集合。每条 `document_summaries` 同时保存稳定 `document_id`，社区级 `source_document_ids` 与摘要列表保持一致。没有任何 chunk 关联时仍保留原有有界前四份文档回退，但不会把全体同名文件扩进来。

## 外部实践与本地映射

- [Microsoft GraphRAG BYOG 输入约定](https://github.com/microsoft/graphrag/blob/main/docs/index/byog.md)要求实体/关系携带 `text_unit_ids`；本地把实体 chunk provenance 作为社区到文档摘要的唯一收窄依据。
- [Microsoft GraphRAG 社区报告回指边界](https://github.com/microsoft/graphrag/discussions/931)指出聚合社区报告不能冒充句子级原文引用；本轮让摘要集合更准确，但仍要求最终回答回到原始 chunk evidence。
- [LlamaIndex 节点来源关系](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py)区分 node 与 source/ref document；SoulDrive 同样不把文件名显示值当作文档身份或 provenance。

## 失败优先与修复

- 反例构造两个同名 `report.pdf`：`doc-a` 只含 `chunk-a`，`doc-b` 只含 `chunk-b`；社区实体只引用 `chunk-a`。
- 旧实现 `source_document_ids` 为 `doc-a/doc-b`，摘要列表同时含项目 A/B；修复后两者均只含 `doc-a`。
- `document_summaries` 新增稳定 `document_id`，用户可读 `source_filename` 保留；旧索引没有该字段时仍可读取。

## 已验证

- 失败优先反例先失败，修复后 GraphRAG、两种图存储和 Context Gate 定向回归：`77 passed`。
- 社区 provenance 改动后的最新完整工程门为后端 `1443 passed, 1 warning`、前端 8 组脚本 `62 passed`、锁定 Cargo 通过；此前比较分解改动后的工程门为后端 `1441 passed, 1 warning`。

## 边界

- 该改动证明社区摘要集合不再因同名文件扩大，不证明实体抽取、社区摘要语义或最终答案准确率提升。
- 不把社区摘要升级为原文引用，不引入远程 GraphRAG、Neo4j 或新的 Agentic RAG 状态机。
