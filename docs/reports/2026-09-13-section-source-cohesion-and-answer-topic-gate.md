# 章节来源一致性与回答主题保真优化

日期：2026-09-13

## 目标

继续检查章节型问题在“检索命中正确但 Top-K 混入其他来源”以及“证据已支持但本地模型回答泛化掉引号内主题”两个边界。目标不是增加新的 Agent，而是把已有结构路由、父子块上下文和回答质量门连成一条可验证链路：

`章节/主题识别 -> 唯一高置信来源约束 -> 局部子块上下文 -> 主题保真回答 -> 引用/支持门`

## 外部实践对照

- [Chroma collection configuration](https://docs.trychroma.com/docs/collections/configure?lang=typescript) 将 HNSW 近似近邻检索的召回与查询成本作为可调参数；本轮没有直接改动 `ef_search`，而是先用可解释的结构来源门减少已知的跨来源噪声，避免把一次评测结果误归因于向量参数。
- [Haystack retrievers](https://docs.haystack.deepset.ai/docs/retrievers)、[DocumentJoiner](https://docs.haystack.deepset.ai/docs/documentjoiner) 与 [metadata filtering](https://docs.haystack.deepset.ai/docs/metadata-filtering) 将过滤、召回和合并分开；本轮把“唯一章节来源约束”放在排序选择阶段，且对多来源/需要来源多样性的任务保留普通路径。
- [LlamaIndex similarity postprocessor](https://docs.llamaindex.ai/en/v0.12.15/api_reference/postprocessor/similarity/) 体现了召回后仍需做质量门；本轮在生成前增加引号主题保真检查，缺失时复用已有证据回退，不让模型凭空补齐。

## 实现

### 1. 结构路由的来源一致性

`core/retrieval.py` 新增唯一高置信章节来源门：只有候选均来自同一 `source_key`、且章节覆盖率达到完整覆盖时才收窄到该来源；来源不唯一、查询要求来源多样性或没有精确章节命中时不强行收窄。收窄后优先精确节点，再选同源上下文，并把门控结果写入诊断字段。

这避免了精确章节已经找到，但因为其他来源的高分噪声挤入有限引用窗口的问题；也避免把实验对比等显式多来源任务错误变成单来源回答。

### 2. 父子块上下文局部化

`core/knowledge_base.py` 对完整章节路径命中使用 `section_route_child_window`，优先返回命中的子块窗口，并保留章节、页码、起止位置等 provenance。普通查询仍使用既有父文档上下文，不改变通用召回路径。

### 3. 回答中的引用主题保真

`core/rag_engine.py` 在系统提示中要求保留引号内章节/主题/限定词；`generated_answer_needs_fallback()` 在检测到回答没有覆盖查询中的引号主题时，转入已有证据回退回答。该规则只检查查询显式给出的主题，不硬编码评测集标签。

## 失败优先与回归

- 新增检索反例：同一章节号但不同章节标题不能得到相同的精确结构分。
- 新增来源一致性反例：唯一高置信章节来源应排在同源候选内，显式来源多样性任务不应被收窄。
- 新增上下文反例：章节路径命中不能因为返回整篇父文档而把相邻章节挤入首个证据窗口。
- 新增回答反例：模型输出没有引号主题时必须走已有证据回退，并带回主题和引用。

当前验证结果：

- 冻结开发集 16 条：全命中@6、任一命中@6、首条命中@1、MRR、来源覆盖率、上下文精确率均为 `100%`。
- 冻结 sealed 集 8 条：上述检索指标均为 `100%`，MRR `1.000`。
- 开发集答案链 16 条、sealed 答案链 8 条：引用合法率、引用支持率、综合可靠率、关键词覆盖率均为 `100%`；不支持 claim 为 `0`。
- 结构路由、知识库路径和回答提示专项回归通过；随后 `scripts/check.ps1` 完成 `1346 passed, 1 warning`，前端构建与 8 组 UI 脚本共 `61` 项通过，`compileall` 与 `cargo check --locked` 通过。
- GraphRAG 路由专项 `4/4`，记忆质量专项 `6/6` 场景、`7/7` 查询，研究窗口角色专项 `6/6`；记忆质量评测的过期/越权泄漏率为 `0`。

结果文件：

- `eval/results/rag_cn_raw_v0.1_cold_section_priority2_20260913.jsonl`
- `eval/results/rag_cn_raw_v0.1_dev_section_topic_gate_answers_20260913.jsonl`
- `eval/results/rag_cn_raw_v0.1_sealed_section_topic_gate_answers_20260913.jsonl`
- `eval/results/graphrag_acceptance_cold_candidate_20260913.json`

## 边界

- 这是本机冻结语料、当前量化模型和自动规则门的证据，不等于人工事实评审、跨模型泛化或生产答案准确率。
- 没有把冷工作区与既有工作区的耗时差异当作性能提升结论；HNSW `ef_search` 也没有在没有同条件基线前擅自改动。
- 只对唯一高置信章节来源做收窄；多来源比较、歧义章节、没有完整覆盖的候选继续走普通路径。
- `scripts/check.ps1` 不证明真实 Tauri/WPS UI、安装包冷启动、跨存储原子提交、外部 exactly-once 或端到端 Agentic RAG 质量。
