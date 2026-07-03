# SoulDrive 严格检索质量评测集 v0.1

## 目标

本评测集用于驱动 `codex/rag-retrieval-quality` 分支上的 RAG/IG 检索质量优化。它不评估 UI、API 网关、打包脚本或工程卫生，只评估本地知识引擎在给定企业知识资料上的检索召回、跨文档召回和图谱相关召回能力。

## 主指标

主指标采用更严格的 `retrieval_all_hit_rate@3`。

一条样本只有在 top 3 证据中命中全部 `expected_sources` 时才算通过。跨文档样本通常包含 2 到 3 个期望来源，因此不能只命中其中一个来源就算成功。

建议门槛：

- `retrieval_all_hit_rate@3 >= 80%`：达到可合并候选门槛。
- `retrieval_all_hit_rate@3 >= 90%`：达到优秀检索质量。
- `first_hit_rate@1`、`mean_reciprocal_rank`、`average_source_coverage@3` 不应明显下降。

## 样本构成

- 文件：`eval/retrieval_quality_strict_v0.1.jsonl`
- 总样本数：60
- 单文档/安全问题：30
- 跨文档严格 all-hit 问题：22
- 图谱相关问题：20
- 拒答问题：8
- 覆盖本地资料源：12 个以上

## 使用方式

先准备或解锁包含目标资料的 SoulDrive 工作区，然后运行：

```powershell
conda run -n souldrive python scripts/eval_rag.py --dataset eval/retrieval_quality_strict_v0.1.jsonl --workspace <SoulDrive工作区路径> --top-k 3
```

如果工作区已在桌面端解锁，也可以省略 `--workspace`，由评测脚本读取当前运行态。

## 后续优化节奏

1. 跑当前基线，保存结果。
2. 导出未命中样本，按单文档、跨文档、图谱、拒答分类。
3. 只针对检索链路做一组小改动。
4. 重新跑本评测集，比较 `retrieval_all_hit_rate@3`、`first_hit_rate@1`、`MRR` 和来源覆盖率。
5. 指标稳定达到门槛后，再考虑合并回主分支。

## 后续优化候选清单

以下清单只服务于 RAG/IG 检索质量提升，不包含 UI、API 网关、打包脚本或通用工程卫生。

### 1. 未命中诊断与基线报告

先扩展评测输出，不改检索算法。目标是让每条失败样本能解释为什么失败。

- 输出每条样本的 `expected_sources`、top 3 实际来源、rank、score breakdown 和缺失来源。
- 将失败归类为：完全无召回、只命中部分来源、首条错误、跨文档不全、图谱相关缺失、source alias 未匹配、拒答样本误召回。
- 保存 baseline JSONL，作为后续每次优化的比较对象。
- 主指标固定为 `retrieval_all_hit_rate@3`，避免只看任一命中率导致虚高。

### 2. 文档切片与上下文增强

当前检索链路已经有 parent-child 结构，但 child chunk 固定尺寸会丢失标题、相邻段落和表格上下文。优先优化切片，不直接换模型。

- 为每个 child chunk 注入完整标题路径，例如 `Header 1 > Header 2 > Header 3`。
- 对短章节保留完整章节，对长章节再切 child，避免语义段被硬切断。
- 检索时用小块召回，返回时扩展到父块或相邻窗口，提升答案证据完整性。
- 对表格、清单、标准条款类内容增加稳定的编号/条款保留策略。
- 评测重点：单文档题、标准安全题和条款型问题的 `first_hit_rate@1`。

### 3. 查询扩展与多查询召回

当前查询扩展主要是少量硬编码术语。严格评测集里有大量同义表达、跨文档比较和安全治理问题，需要更系统的查询变体。

- 对问题生成 3 到 5 个本地规则变体：原问题、关键词压缩、术语扩展、文档类型提示、问题意图提示。
- 对跨文档问题生成子查询，例如“数据分类分级规则怎么说”和“生成式 AI 安全要求怎么说”。
- 对安全治理、RAG、知识库、数据要素、城市 AI、智能体等主题建立可维护术语表。
- 多查询结果用 RRF 融合，避免单一 query miss 后全链路失败。
- 评测重点：跨文档样本的 `retrieval_all_hit_rate@3` 和 `average_source_coverage@3`。

### 4. 稀疏检索与中文词项改进

当前 BM25 tokenization 对中文按单字切分，能召回但区分度有限。需要让中文术语、文件名、标题和条款词更有权重。

- 增加中文词组索引，至少覆盖评测集中的资料名、主题词和安全术语。
- 将 `source_filename`、标题路径、章节名作为可检索字段参与 sparse score。
- 对问题中出现的资料名别名加 source boost，但不能直接跳过内容相关性。
- 保留 dense retrieval，稀疏检索只做补召回和排序辅助。
- 评测重点：资料名明确或主题词明确的样本不能 miss。

### 5. 跨文档召回与来源多样性

严格主指标要求跨文档题 top 3 命中全部来源。单纯按最终分数排序可能让同一来源的多个 chunk 挤占 top 3。

- 在最终候选选择阶段加入 source diversity，避免同一文件占满 top 3。
- 对 `requires_multi_doc` 类型查询，优先保留不同来源的高分候选。
- 对跨文档比较题，引入子查询级来源覆盖统计，再融合到最终 top 3。
- 不改变普通单文档题的排序策略，避免为了跨文档牺牲单文档首条命中。
- 评测重点：SRQ-021 到 SRQ-042 的 all-hit 表现。

### 6. 图谱相关召回补强

图谱相关样本不要求生成完整图谱答案，但要求检索结果能覆盖实体、关系和相关来源。

- 图谱抽取阶段记录实体别名、关系文本和来源文件。
- 检索时把 query 中的实体词、资料名和领域词映射到 graph hints。
- graph hints 只做候选补充和轻量 boost，不应覆盖 dense/sparse 的证据相关性。
- 修正乱码图谱上下文文案后，再把图谱 hit 纳入失败分析。
- 评测重点：`graph_relevant=true` 样本的来源覆盖率和 top 3 多样性。

### 7. Reranker 与融合权重评测

当前已有可选 reranker 和手写融合权重。下一步不是凭感觉调参，而是用严格评测集做小范围网格比较。

- 比较不同 dense pool size：18、30、50。
- 比较不同 RRF k：30、60、90。
- 比较 dense、sparse、graph、rerank、source boost 的权重组合。
- 保存每组配置的指标和失败样本变化。
- 只有在 `retrieval_all_hit_rate@3` 提升且 `first_hit_rate@1` 不明显下降时保留配置。

### 8. 成功标准

本分支后续优化到以下水平才建议合并：

- `retrieval_all_hit_rate@3 >= 80%`。
- `retrieval_any_hit_rate@3 >= 90%`。
- 跨文档样本 `retrieval_all_hit_rate@3 >= 75%`。
- 图谱相关样本 `retrieval_any_hit_rate@3 >= 85%`。
- 没有为了命中率牺牲拒答样本的证据边界。
