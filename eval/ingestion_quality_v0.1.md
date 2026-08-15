# 文档清洗、切片、入库质量评测 v0.1

该评测集只验收 ingestion 链路，不替代检索盲测。

## 覆盖范围

- 216 条样本，覆盖语料清单中的全部 36 份文档。
- 每份文档固定 6 个检查面，单条样本权重约 0.46%。
- 覆盖企业技术方案、项目方案、政府政策、国家标准及中英文 RAG 研究文献。
- 包含 131 条 hard 样本、11 类评测 scope、58 个 focus 标签。
- 表格样本 56 条、图片样本 58 条、多模态样本 46 条、深切片样本 103 条。
- 表格与图片要求来自独立 PDF 页级审计，不再按主题标签推测；无嵌入模态的正式文件仍保留文本版式检查。

每份文档的 6 个检查面：

1. 结构与章节切分。
2. 两段滑窗与单段重叠。
3. 上下文增强与来源溯源。
4. 父子索引、语义单元和模型/Schema 版本。
5. 表格、图片、清单或条款保真。
6. 正文编码与控制字符完整性。

## 核心指标

- `document_coverage_rate`：36 份文档是否均有可检索切片。
- `section_metadata_rate`：章节、标题层级是否保留。
- `paragraph_overlap_rate`：相邻窗口是否存在有效重叠。
- `formal_window_rate`：正式文件是否采用“两段一块、重复一段”。
- `retrieval_context_rate`：切片是否带文档标题和章节语境。
- `source_provenance_rate`：文件名、文档哈希、Schema 是否可追溯。
- `parent_linkage_rate`：父子切片标识是否完整。
- `semantic_unit_rate`：语义单元计数是否有效。
- `index_schema_rate`：索引 Schema 和嵌入模型版本是否记录。
- `token_safe_rate`：实际嵌入输入是否未超过模型上限。
- `table_coverage_rate`、`image_metadata_rate`：结构化内容是否保留。
- `content_integrity_rate`：正文是否非空、无替换字符和非法控制字符。

## 运行

先验证评测集自身的广度与深度：

```powershell
conda run --no-capture-output -n souldrive python scripts/eval_ingestion.py --dataset eval/ingestion_quality_v0.1.jsonl --audit-dataset-only
```

语料变化后先重建表格/图片页级真值，再生成评测集：

```powershell
conda run --no-capture-output -n souldrive python scripts/build_corpus_modality_ground_truth.py --corpus-dir <PDF 目录>
conda run --no-capture-output -n souldrive python scripts/build_ingestion_eval_dataset.py
```

当前质量门禁基线：

```text
评测集质量门禁: 通过 (100.0%)
评测集覆盖: 样本=216, 文档=36, 每文档最少样本=6, 文档类=5, 语言=2, scope=11, hard样本=131, focus标签=58, 表格样本=56, 图片样本=58, 多模态样本=46, 深切片样本=103
```

验收真实 workspace：

```powershell
conda run --no-capture-output -n souldrive python scripts/eval_ingestion.py --workspace <SoulDrive workspace>
```

统一执行 ingestion 与 retrieval Harness：

```powershell
conda run --no-capture-output -n souldrive python scripts/eval_harness.py --workspace <SoulDrive workspace> --retrieval-results eval/results/retrieval_candidate_v0.3.jsonl
```
