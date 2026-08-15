# 正式文档结构检索评测集 v0.4

该数据集用于回归验证正式文档的结构检索、候选融合和证据选择，不替代 36 份真实语料上的盲测。

## 规模

- 360 个检索问题，3600 个候选切片。
- 6 个类别，每类 60 条。
- easy 90 条、medium 90 条、hard 180 条。
- 同一事实的重叠切片通过 `expected_groups` 标为等价证据，避免仅因 chunk ID 不同产生假阴性。

类别：

1. `clause_lookup`：条款号与章节定位。
2. `table_lookup`：表格、指标、责任部门和验收依据。
3. `document_reference`：文件名、文号和标准号。
4. `overlap_dedup`：两段滑窗重叠与等价证据去重。
5. `cross_source_recall`：跨文档双来源召回。
6. `broad_summary`：总体要求、实施步骤和验收内容。

## 当前结果

| 指标 @3 | Dense 基线 | 混合结构检索 |
|---|---:|---:|
| 任一命中率 | 54.2% | 100.0% |
| 全证据组命中率 | 25.0% | 100.0% |
| 平均证据组召回率 | 39.6% | 100.0% |
| MRR | 0.438 | 1.000 |

360/360 的 95% Wilson 置信区间为 `[98.9%, 100.0%]`，因此报告不会把有限样本上的 100% 解释为真实系统的绝对 100%。

这些结果只证明结构路由、来源约束和去重规则在确定性对抗集上符合预期。真实质量以 `retrieval_quality_blind_v0.3.jsonl` 的 360 问盲测为准。

## 重建

```powershell
conda run --no-capture-output -n souldrive python scripts/build_structured_retrieval_eval_dataset.py --top-k 3
```
