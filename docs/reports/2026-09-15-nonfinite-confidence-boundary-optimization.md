# 检索与多模态置信度非有限值边界优化

日期：2026-09-15  
范围：混合检索入口、项目相关性回退、OCR、Topic5 事件、SoulAlign、记忆写入与学习候选校验

## 外部实践与真实缺口

本轮继续对齐优质开源实现的两个边界：

- [Haystack SentenceWindowRetriever](https://github.com/deepset-ai/haystack/blob/main/haystack/components/retrievers/sentence_window_retriever.py) 把 `source_id`/`split_id` 当作显式来源元数据，而不是把检索结果的展示字段直接当事实；
- [LlamaIndex Node/Source metadata](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py) 与 [Citation Query Engine](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/query_engine/citation_query_engine.py) 将节点身份、分数和引用来源分开管理；
- [Microsoft GraphRAG provenance dataflow](https://github.com/microsoft/graphrag/blob/main/docs/index/default_dataflow.md) 要求派生知识能回指原始 text unit，异常的排序信号不能替代来源事实。

静态扫描发现，SoulDrive 的上游检索和多模态/记忆入口仍有同一类 Python `min/max` 边界错误：`NaN` 常会被当作 0，但 `Infinity` 可能被夹成 1.0。具体影响为：

- 混合检索的 reranker、GraphRAG 分数、向量距离、项目相关性和质量诊断可能被异常值污染；
- OCR、Topic5 事件和 SoulAlign 可能把无效感知当作高置信目标；
- 记忆写入门与学习候选校验可能把异常置信度当作满分，绕过应有的人工审核强度。

## 失败优先与最小实现

新增并先运行以下反例：

1. `_bounded_score()`、混合排序和质量诊断收到 `NaN/±Infinity`；
2. 项目相关性收到 `vector_distance=NaN`；
3. OCR、反思、Topic5 公共/融合事件收到 `Infinity`；
4. SoulAlign、学习候选和记忆写入门收到 `NaN/Infinity`。

修复保持现有正常值语义：

- `core/retrieval.py` 增加有限数值归一化；排序前清理 GraphRAG/reranker/vector distance，权重和诊断不再输出非有限值；
- `core/knowledge_base.py` 的项目相关性仅对有限 dense distance 使用相似度，异常距离不能制造满分项目命中或阻止全局回退；
- `core/image_ocr.py`、`core/topic5_runtime.py`、`core/soulalign.py` 对无效置信度按 0 处理；正常缺省字段仍保持原有默认值；
- `core/reflection.py`、`core/learning_validator.py`、`core/memory_gate.py` 的置信度夹具统一 fail-closed。

## 回归与评测证据

- 跨模块失败优先修复后的定向组合：`304 passed, 1 warning`，覆盖 RAG/GraphRAG、知识库检索、安全检索、OCR、Topic5、SoulAlign、记忆门和学习校验。
- 本轮代码变更后的完整工程门禁：后端 `1567 passed, 1 warning`；前端生产构建成功，UI 脚本 `62 passed`；Python `compileall` 与锁定 Cargo 检查通过。
- 冻结记忆质量：`7/7` 场景、`8/8` 查询，`memory_recall_at_k=1.0`，陈旧/禁止泄漏率 `0.0`。
- GraphRAG 路由：`4/4`，route/graph-gate/signal-contract 均 `1.0`。
- 研究窗口 episodic：runtime `5/5`，baseline `2/5`；本轮保持既有窗口隔离收益。

## 边界

本轮证明非有限数值不会通过已覆盖的检索排序、项目回退、感知置信度、目标绑定或记忆写入边界；不等于真实 OCR/VLM 识别质量、召回率、答案事实忠实度、真实 Redis/PostgreSQL、跨进程 exactly-once、长时间性能、打包 Tauri/WPS UI/E2E 已完成验收。
