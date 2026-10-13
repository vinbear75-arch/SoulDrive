# 4 条候选支持项：独立 gold/标签草案 v0.1

日期：2026-10-13。状态：待人工标签审核；不是冻结集或评分结果。

用户已确认这 4 条的候选来源与定位，并授权仅为它们制作标签草案；随后批准仅在独立审核包采用 RAG-002/PAPER-001 的候选改题。本轮在 `eval/review_drafts/business_seed_four_v0.1/label_drafts.json` 单独保存；原 30 条题目、`source_refs`、`setup`、`gold=null`、manifest 和现有冻结集均不改。其余 17 条部分支持、9 条缺口不生成 gold。改题许可不等于标签批准或冻结许可；未评分、训练、调参、扩集或修改指标。

## 审核索引

| Seed | 标签/答案草案概要 | 必须保留的边界 |
| --- | --- | --- |
| SEED-RAG-002 | 区分 BM25 关键词召回、Joiner 结果融合、按既有分数排序、Self-RAG 生成支持性评价；每个命题有固定文件行号或 PDF 页码。 | 原题仍点名 BIMSA/旧三大模块材料；新来源不是这些原文。候选改题只在独立审核包获准采用，不能作为原题完整 gold。所列组件不足以证明密集向量路由或学习式重排已配置。 |
| SEED-PAPER-001 | 按研究问题、核心方法、实验结果、局限、复现及汇报提纲组织；明确五类能力与七种题型不同。 | LongMemEval 是研究论文，不是原题选中综述。新题/来源只用于独立审核包，不改原种子的选中资产。只核验第 1-4、10 页，不宣称全文复现。 |
| SEED-MEM-003 | 成功且生效的同项目更新后，当前值为 v2；v1 可作为明确标注的历史事实。 | 旧缓存故障尚未具体化，不给该变体评分；外部项目不能证明本地历史记录、恢复或 exactly-once 已存在。 |
| SEED-MEM-004 | 合成有效时间 `as_of=100 -> v1`、`as_of=300 -> v2`；新值从 200 生效。 | 初始 `valid_from=null` 按现有代码表示有效时间下界不限，不是补造现实日期。有效时间不等于记录/知情时间；历史读取新鲜度需单独观测。 |

## 来源复查

- 重新计算 7 份所用外部文件及本地事实合同的 SHA-256；字节指纹与已批准绑定包相符。JSON 保留固定 commit/论文版本、原始 URL、相对路径和哈希。
- 对两篇 PDF 的相关完整页面进行渲染和正文核对：LongMemEval 第 1-4、10 页，Self-RAG 第 1-4 页；包括表题、行列条件、脚注和算法说明，不只读摘要。临时 PNG 仅用于阅读核验，不修改原 PDF。
- LongMemEval 第 10 页 Table 4 的具体草案命题：在 `LongMemEval_M` 时间推理子集、`Value=Session`、`K=V+fact` 下，GPT-4o 时间查询扩展的 Recall@10 为 `0.721 -> 0.797`，NDCG@10 为 `0.782 -> 0.758`。因此标签明确禁止“所有指标都提升”的概括；不是 SoulDrive 的实测成绩。[固定论文版本](https://arxiv.org/abs/2410.10813v2)
- Self-RAG 的反思评价与 Joiner 的融合/排序不是同一机制，也不能仅靠提示词模仿就宣称实现了论文的训练方法。[固定论文版本](https://arxiv.org/abs/2310.11511v1)
- 本地初始 NULL 有效边界已回查 `LearningStore` 的持久化、区间筛选和历史关闭代码，以及原有 `MEM-TEMPORAL-001` 合同；不增设日期、查询样本或新指标。

## 下一道审核门

1. 审阅每条 `gold_draft`、`labels_draft` 和 `unresolved`，可按 claim ID（如 `R2-3`、`P1-3`）提出修改。
2. RAG-002/PAPER-001 的候选改题已获准在独立审核包采用：JSON 的 `adopted_prompt_field` 指向候选题，`review_selected_source_refs` 指定审核材料；原题、原 source_refs 和原选中资产未替换。标签本身仍待审核。
3. 补齐缓存故障和历史读取新鲜度的可观察设置后，才能提出相应变体标签；不提前认定通过。
4. 即使草案获认可，数据冻结、指标/划分、业务 baseline、模型评分和训练仍须分别满足项目审核门，不由本次授权推导。

## 结构与指纹校验记录

候选改题授权落入独立审核包后，再次只读校验通过：4 条标签草案、2 条获准采用的审核题、9 份来源指纹；原 30 条均 `gold=null`，原题/来源与原 manifest 哈希未变。`gold_labels_approved=false`、`freeze_allowed=false`、`scoring_performed=false`，未指定 train/validation/test 划分。原包的 `validate_business_seed_review.py` 退出 0、`ready_for_freeze=false`。

- 原 `seeds.jsonl` SHA-256：`ce12cad8be0c65a0df0f823d3520ef5fbf669f5e0d8eb46b7a64fb2e63c55424`。
- 原 `manifest.json` SHA-256：`752f72c5fd3f39a26960c6333755af6e807758d06140c205d385e8070ac4612d`。
- 当前独立 `label_drafts.json` SHA-256：`0052d2576abd896bc358dca130f7ee69f6ed9cc1ac383eb14218f21908ec84a7`。

结构与指纹校验只确认草案格式、来源定位和权限边界，不确认答案 gold 正确率；工程回归也不消费这 4 条草案。
