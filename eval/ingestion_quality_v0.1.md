# 文档清洗切片入库质量评测 v0.1

这个评测集专门评估 ingestion 链路，不替代原有检索评测集。

## 文件位置

- 评测集：`eval/ingestion_quality_v0.1.jsonl`
- 指标脚本：`scripts/eval_ingestion.py`
- 统一 Harness：`scripts/eval_harness.py`
- 旧检索主评测集：`eval/retrieval_quality_blind_v0.3.jsonl`

## 评测范围

每条样本对应一份源文档，重点覆盖：

- 企业技术方案和项目方案。
- 政府正式报告、标准、指引、清单。
- 包含表格、流程图、图片、指标表的结构化文档。
- 中英文 RAG 参考文献。

## 核心指标

- `document_coverage_rate`：评测文档是否在索引中存在切片。
- `chunk_count_pass_rate`：每份文档的最小切片数是否达标。
- `page_metadata_rate`：切片是否保留页码。
- `section_metadata_rate`：切片是否保留标题/章节信息。
- `dynamic_chunk_rate`：切片是否使用 `dynamic_structure_overlap_v1`。
- `paragraph_overlap_rate`：需要段落重叠的文档是否出现有效 overlap。
- `table_coverage_rate`：需要表格的文档是否保留表格结构。
- `image_metadata_rate`：需要图片/流程图的文档是否存在图片元数据。
- `overall_ingestion_quality_rate`：上述要求按样本聚合后的整体达标率。

## 单独运行 ingestion 评测

```powershell
conda run --no-capture-output -n souldrive python scripts/eval_ingestion.py --workspace <SoulDrive workspace>
```

如果 workspace 是加密索引：

```powershell
conda run --no-capture-output -n souldrive python scripts/eval_ingestion.py --workspace <SoulDrive workspace> --passphrase <workspace password>
```

也可以用离线 chunk JSONL 调试指标：

```powershell
conda run --no-capture-output -n souldrive python scripts/eval_ingestion.py --dataset eval/ingestion_quality_v0.1.jsonl --chunks-jsonl eval/results/indexed_chunks_candidate.jsonl
```

## 统一 Harness

统一 Harness 会把旧检索评测和新的 ingestion 评测聚合在一起，作为后续方案取舍的总入口。

使用已保存检索结果和当前 workspace：

```powershell
conda run --no-capture-output -n souldrive python scripts/eval_harness.py --workspace <SoulDrive workspace> --retrieval-results eval/results/retrieval_candidate_v0.3.jsonl
```

完整输出 JSON：

```powershell
conda run --no-capture-output -n souldrive python scripts/eval_harness.py --workspace <SoulDrive workspace> --retrieval-results eval/results/retrieval_candidate_v0.3.jsonl --save-report eval/results/rag_harness_candidate_v0.1.json
```

## 当前边界

表格会以 Markdown 和 `tables_json` 元数据保留到切片 metadata 中。图片/流程图先记录 `image_metadata_json` 和 `vision_status=pending_local_model`，不在本轮引入联网视觉模型。后续接入本地视觉模型时，应把视觉描述写回同一字段并继续使用本评测集比较。
