# 章节标题结构路由优化记录

日期：2026-09-13

## 结论

本轮修复了一个可复现的 RAG 排序问题：查询同时包含章节号和章节标题时，系统不能只因为候选章节号相同就授予完整的结构匹配分。修复后，同一份 16 条开发集的首条命中率从 93.8% 提升到 100%，MRR 从 0.969 提升到 1.000，上下文精确率@6 从 87.9% 提升到 96.4%。全命中率@6 保持 100%。

这是一项检索排序改进，不代表答案生成质量、引用支持率或真实桌面端链路已经完成验收。

## 失败样本

评测数据：`eval/fixtures/rag_cn_raw_v0.1/dev.jsonl`，16 条，数据指纹：
`sha256:c8fe86507ec7b01b46d1598870639c5aff93c670d0efbbac6fc424ad0cf28c99`。

失败样本：`RAGCN-93128a5b23df-04`

查询为：

> 请梳理资料中“2.1 RAG 范式”对应的背景、方法或结论结构，不要补充资料外事实。

基线把 `06_检索增强生成_SciOpen.pdf` 的“2.1 RAG的原理及技术框架”排在第一条；目标文档 `02_面向知识密集型任务的检索增强生成技术综述.pdf` 的对应内容排在第二、三条。原因不是目标文档未召回，而是归一化后的章节号 `2.1 -> 21` 被当作完整结构命中，抵消了章节标题不一致。

## 对齐的公开实践

- Haystack 将文档元数据过滤作为检索管线中的显式边界，允许在检索前按元数据缩小候选集合，并把过滤与排序职责区分开：[Metadata Filtering](https://docs.haystack.deepset.ai/docs/metadata-filtering)、[Retrievers](https://docs.haystack.deepset.ai/docs/retrievers)。本项目没有照搬框架，只把同一原则用于已有的章节路径元数据：结构标题不匹配时不授予“精确章节”分。
- Microsoft GraphRAG 的 Local Search 先形成候选文本单元，再进行 ranking + filtering，结构化实体上下文与文本单元不是无条件混合：[Local Search](https://github.com/microsoft/graphrag/blob/main/docs/query/local_search.md)。本项目沿用“候选生成后仍需结构过滤”的边界，没有因此新增 GraphRAG 路由。
- LangChain 的 ParentDocumentRetriever 保留子块命中后回查父文档的关系：[ParentDocumentRetriever](https://reference.langchain.com/python/langchain-classic/retrievers/parent_document_retriever)。本次没有改变现有父子扩展，只修正父子扩展前的章节判定。

## 实现范围

- `core/retrieval.py`：当查询包含带标题的十进制/编号章节，并且候选未通过章节路径、标题或子块标题回退匹配时，不再因归一化章节号相同而设置 `structure=1.0`。
- 正式条款（如“第三条”）以及已经通过章节路由匹配的候选保持原有行为。
- `tests/test_retrieval.py`：增加失败回归，明确区分“同章节号但标题不同”和“子块标题实际命中”。

## 验收证据

### 单元回归

- 失败测试先复现：目标和错误候选均得到 `structure=1.0`。
- 修复后：新增回归通过；`tests/test_retrieval.py` 共 40 passed。

### 同源开发集对比

基线结果：`eval/results/rag_cn_raw_v0.1_dev_current_20260913.jsonl`

候选结果：`eval/results/rag_cn_raw_v0.1_dev_candidate_20260913.jsonl`

| 指标 | 基线 | 候选 | 变化 |
|---|---:|---:|---:|
| 全命中率@6 | 100.0% | 100.0% | 0.0 个百分点 |
| 首条命中率@1 | 93.8% | 100.0% | +6.2 个百分点 |
| MRR | 0.969 | 1.000 | +0.031 |
| 来源覆盖率@6 | 100.0% | 100.0% | 0.0 个百分点 |
| 上下文精确率@6 | 87.9% | 96.4% | +8.4 个百分点 |

同一候选结果的离线 Top-K 汇总为：Top-3 上下文精确率 100.0%，Top-4/5 为 98.4%，Top-6 为 96.4%。该扫描只用于观察，不足以授权改变生产默认 Top-K；它没有覆盖答案生成、拒答、跨项目或图谱题。

### Sealed 泛化复核

在开发集候选冻结后，使用未参与调参的 `eval/fixtures/rag_cn_raw_v0.1/sealed.jsonl` 独立复核 8 条样本，数据指纹为 `sha256:0e51d24f551d87ae13b681575bca8bbc041456fd4176803176147b607093a5d4`。结果为：全命中率@6 `100.0%`、任一命中率@6 `100.0%`、首条命中率@1 `100.0%`、MRR `1.000`、上下文精确率@6 `90.7%`。逐样本结果保存于 `eval/results/rag_cn_raw_v0.1_sealed_candidate_20260913.jsonl`。

Sealed 仍只有检索标签，没有答案生成、引用支持、拒答或 GraphRAG 样本；因此它证明的是当前章节结构约束没有在同源留出集上造成来源召回退化，不是最终答案质量证明。

### 答案链复核

在相同本机模型 `qwen2.5-3b-instruct-q4_k_m.gguf` 下，开发集答案链 16 条与 sealed 答案链 8 条均通过当前自动门：检索全/任一/首条命中均为 `100.0%`，引用合法率与引用内容支持率均为 `100.0%`，综合可靠率与答案关键词覆盖率均为 `100.0%`。开发集检查 37 个 claim，sealed 检查 18 个 claim，均无不支持 claim。

逐样本结果分别保存于 `eval/results/rag_cn_raw_v0.1_dev_answers_candidate_20260913.jsonl` 和 `eval/results/rag_cn_raw_v0.1_sealed_answers_candidate_20260913.jsonl`。这些是同一规则评测器和本机量化模型下的自动结果，不等于人工事实评审、跨模型稳定性或业务答案准确率。

## 边界

- 该开发集只有单来源检索题，没有跨文档全命中、GraphRAG、拒答、答案引用支持率样本。
- 本轮没有使用 `sealed.jsonl` 调参，也没有改变评测数据、索引、嵌入模型或父子切片配置。
- 平均耗时不作为结论：基线是冷启动运行，候选运行受到模型/索引缓存影响，条件不一致。
- 检索指标不等于最终答案正确率；需要后续用带答案/引用的同源集与 sealed 集独立验证。
