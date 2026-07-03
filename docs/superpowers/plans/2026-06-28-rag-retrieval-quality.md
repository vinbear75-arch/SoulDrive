# RAG Retrieval Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不重建索引、不改变文档切片策略的前提下，把 36 篇文档评测集上的严格检索全命中率 `retrieval_all_hit_rate@3` 提升到 80% 以上。

**Architecture:** 本分支只优化查询时链路：查询理解、query expansion、dense/keyword 召回融合、父子块候选合并、rerank、证据去重和引用过滤。导入、切片、embedding 模型、索引 schema、图谱构建属于后续 ingestion-quality 分支，除非单独开新分支并重新建立 baseline。

**Tech Stack:** Python 3.10, unittest, sentence-transformers, Chroma / SecureVectorStore, PowerShell baseline runner

**Evaluation Update:** v0.2 was reclassified as a title-diagnostic set after an
audit found broad full-title leakage. The primary branch gate is now
`eval/retrieval_quality_blind_v0.3.jsonl`, with 300 blind samples and 60
known-title samples.

---

## Files And Responsibilities

- `eval/retrieval_quality_blind_v0.3.jsonl`: 360 条主评测样本，包含 300 条盲测题和 60 条已知标题题，主指标是 `retrieval_all_hit_rate@3`。
- `eval/retrieval_quality_blind_v0.3.md`: 评测集说明、导入、preflight、baseline 和 candidate 对比命令。
- `scripts/import_rag_eval_corpus.py`: 把 36 篇本地 PDF 导入指定 SoulDrive workspace；只在用户显式运行时修改 workspace。
- `scripts/check_rag_eval_workspace.py`: 检查 workspace 是否已包含 v0.2 corpus。
- `scripts/run-rag-eval.ps1`: 固化 baseline/candidate 评测入口。
- `scripts/eval_rag.py`: 评测执行、结果保存、指标汇总和 baseline/candidate 对比。
- `core/query_expansion.py`: 查询改写和术语别名扩展。
- `core/knowledge_base.py`: 候选池生成、dense/keyword 召回、query variants、reranker 接入、父块展开。
- `core/retrieval.py`: BM25、RRF、混合打分、source/title 信号、citation 生成。
- `core/reranker.py`: 本地 cross-encoder reranker 的发现、加载和降级。
- `tests/test_query_expansion.py`: 查询改写单元测试。
- `tests/test_retrieval.py`: 混合排序、citation 和 source 信号测试。
- `tests/test_secure_vector_store.py`: 加密向量库下的真实 search path 测试。
- `tests/test_eval_assets.py`: 评测集规模、覆盖和指标约束测试。
- `tests/test_rag_eval_corpus_import.py`: v0.2 corpus 导入脚本测试。

## Success Gates

- 主门槛：`retrieval_all_hit_rate@3 >= 80.0%`。
- 辅助门槛：`multi_doc_all_hit_rate@3 >= 72.0%`。
- 辅助门槛：`first_hit_rate@1 >= 65.0%`。
- 辅助门槛：`graph_context_hit_rate@3 >= 70.0%`，如果当前 workspace 已有可用图谱上下文。
- 稳定性门槛：同一个 baseline workspace 上连续运行 3 次，主指标波动不超过 2 个百分点。
- 性能门槛：平均检索耗时不超过 baseline 的 1.5 倍；如果指标大幅提升但耗时超限，需要显式记录 tradeoff。
- 隐私门槛：任何新日志、debug 输出、评测结果都不得写出本地绝对源文件路径、workspace passphrase、原文全文或解密密钥。

## Scope Boundary

本计划允许修改：

- 查询改写策略。
- dense/keyword 召回池大小和融合方式。
- RRF、BM25、source/title、graph、reranker 的权重和组合。
- 父子块候选合并、source 去重、证据多样性。
- 评测诊断脚本和结果对比脚本。

本计划不允许修改：

- `core/document_parser.py` 的切片策略。
- `core/indexer.py` 的入库结构。
- `core/parent_child_index.py` 的 child size / overlap。
- embedding 模型、向量维度、索引 schema。
- 已导入 workspace 的向量库内容。

如果后续要优化切片、重建向量库或改 embedding 模型，应从当前分支或 main 再开一个 ingestion-quality 分支，先保存当前 baseline，再清空索引、重新导入 36 篇文档并建立新 baseline。

## Task 1: Lock Baseline

**Files:**
- Verify: `eval/retrieval_quality_blind_v0.3.jsonl`
- Verify: `eval/corpus_manifest_v0.2.json`
- Run: `scripts/import_rag_eval_corpus.py`
- Run: `scripts/check_rag_eval_workspace.py`
- Run: `scripts/run-rag-eval.ps1`

- [x] **Step 1: Preview corpus import**

Run:

```powershell
conda run -n souldrive python scripts/import_rag_eval_corpus.py --workspace <SoulDrive workspace> --dry-run
```

Expected:

```text
expected_documents: 36
missing_files: 0
```

- [ ] **Step 2: Import and index corpus**

Run:

```powershell
conda run -n souldrive python scripts/import_rag_eval_corpus.py --workspace <SoulDrive workspace> --index --skip-graph
```

Expected:

```text
missing_files: 0
```

- [ ] **Step 3: Check workspace coverage**

Run:

```powershell
conda run -n souldrive python scripts/check_rag_eval_workspace.py --workspace <SoulDrive workspace>
```

Expected:

```text
present_documents: 36
missing_documents: 0
```

- [ ] **Step 4: Save retrieval baseline**

Run:

```powershell
.\scripts\run-rag-eval.ps1 -Workspace <SoulDrive workspace> -ResultPath eval/results/retrieval_baseline_v0.3.jsonl
```

Expected:

```text
评测样本数: 360
检索全命中率@3: <baseline value>
```

## Task 2: Add Miss Diagnostics

**Files:**
- Create: `scripts/analyze_rag_eval_misses.py`
- Modify: `tests/test_eval_assets.py`

- [x] **Step 1: Write failing tests**

Add a test that builds a tiny dataset and result file, then verifies the analyzer reports:

- records whose expected source never appears in top 3
- missed source names grouped by filename
- multi-document records that only hit one expected source
- top returned sources for manual inspection

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_eval_assets -v
```

Expected: FAIL because `scripts/analyze_rag_eval_misses.py` does not exist yet.

- [x] **Step 2: Implement analyzer**

Implement CLI:

```powershell
conda run -n souldrive python scripts/analyze_rag_eval_misses.py --dataset eval/retrieval_quality_blind_v0.3.jsonl --results eval/results/retrieval_baseline_v0.3.jsonl
```

Output sections:

- overall miss count
- misses by `type`
- misses by expected source
- worst multi-doc misses
- sample-level top sources

- [x] **Step 3: Verify analyzer**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_eval_assets -v
conda run -n souldrive python scripts/analyze_rag_eval_misses.py --dataset eval/retrieval_quality_blind_v0.3.jsonl --results eval/results/retrieval_baseline_v0.3.jsonl
```

Expected: tests pass and analyzer prints the grouped miss report.

## Task 3: Improve Query Expansion

**Files:**
- Modify: `core/query_expansion.py`
- Modify: `tests/test_query_expansion.py`

- [ ] **Step 1: Write failing tests**

Add tests for these query families:

- policy and standard names: `生成式人工智能服务安全基本要求`, `数据分类分级`
- product/framework names: `MaxKB`, `LazyLLM`, `GraphRAG`
- enterprise RAG terms: `私有化部署`, `权限安全`, `知识库问答`
- city and transport scenarios: `城市+AI`, `综合交通运输大模型`

Expected behavior:

- original query remains first
- at most 5 variants
- technical terms are preserved
- aliases are appended without deleting the user's wording

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_query_expansion -v
```

Expected: FAIL until aliases and bounds are updated.

- [ ] **Step 2: Implement minimal expansion**

Update `TERM_ALIASES` and `expand_query_variants()` so the query variants cover Chinese title aliases, English acronyms, and common phrasing differences. Keep it heuristic and local; do not call an LLM for query rewriting in this task.

- [ ] **Step 3: Verify query expansion**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_query_expansion -v
```

Expected: all query expansion tests pass.

## Task 4: Parent-Aware Candidate Fusion

**Files:**
- Modify: `core/knowledge_base.py`
- Modify: `core/retrieval.py`
- Modify: `tests/test_retrieval.py`
- Modify: `tests/test_secure_vector_store.py`

- [ ] **Step 1: Write failing tests**

Cover:

- candidates from the same `parent_id` merge before final top-k selection
- a strong keyword hit from one source is not lost behind many dense hits from another source
- cross-document questions keep evidence from at least two sources when two sources are competitive

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_retrieval tests.test_secure_vector_store -v
```

Expected: FAIL until fusion becomes parent-aware and source-aware.

- [ ] **Step 2: Implement parent/source merge**

In `LocalKnowledgeBase._merge_candidates()`:

- merge by `parent_id` when available, otherwise by candidate id
- keep the best dense rank, sparse rank, keyword score, vector distance and child excerpt
- preserve source metadata for citation generation

In `rank_hybrid_candidates()`:

- keep RRF as the base score
- add a bounded source-title score
- add a small source diversity bonus only after candidate scoring, not by using eval expected sources

- [ ] **Step 3: Verify candidate fusion**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_retrieval tests.test_secure_vector_store -v
```

Expected: tests pass and existing citation metadata stays path-redacted.

## Task 5: Tune Recall Pool And RRF Weights

**Files:**
- Modify: `core/knowledge_base.py`
- Modify: `core/retrieval.py`
- Modify: `tests/test_retrieval.py`

- [ ] **Step 1: Write failing tests**

Cover:

- dense pool grows with `top_k` but has a hard upper bound
- keyword pool grows with query variants but has a hard upper bound
- final candidate count before parent expansion is larger than `top_k`
- no duplicate final evidence source/page pair is returned

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_retrieval -v
```

Expected: FAIL until pool sizing and duplicate evidence handling are explicit.

- [ ] **Step 2: Implement bounded pool tuning**

Adjust:

- `dense_pool_size = min(max(top_k * 12, 36), 80)`
- keyword pool with the same cap
- final pre-parent rerank pool `max(top_k * 4, 12)`
- duplicate evidence removal by `(source_filename, page, parent_id)`

The exact numbers can be changed after baseline evidence, but every change must be compared with `eval/results/retrieval_baseline_v0.3.jsonl`.

- [ ] **Step 3: Verify pool tuning**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_retrieval -v
```

Expected: tests pass and no duplicate final evidence is emitted.

## Task 6: Strengthen Reranker Signal

**Files:**
- Modify: `core/reranker.py`
- Modify: `core/knowledge_base.py`
- Modify: `core/retrieval.py`
- Modify: `tests/test_reranker.py`
- Modify: `tests/test_secure_vector_store.py`

- [ ] **Step 1: Write failing tests**

Cover:

- reranker receives compact child snippet plus source title, not an oversized parent body
- reranker absence preserves the fallback lexical path
- reranker score cannot fully erase dense/keyword evidence when confidence is weak

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_reranker tests.test_secure_vector_store tests.test_retrieval -v
```

Expected: FAIL until reranker input and weighting are explicit.

- [ ] **Step 2: Implement reranker input builder**

Add a small helper in `core/knowledge_base.py`:

```python
def _reranker_passage(candidate: SearchCandidate) -> str:
    metadata = candidate.metadata or {}
    title = metadata.get("source_filename") or ""
    section = metadata.get("Header 3") or metadata.get("Header 2") or metadata.get("Header 1") or ""
    excerpt = metadata.get("child_excerpt") or candidate.content
    return "\n".join(part for part in [str(title), str(section), str(excerpt)] if part)
```

Use this passage for `LocalReranker.score()`.

- [ ] **Step 3: Verify reranker behavior**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_reranker tests.test_secure_vector_store tests.test_retrieval -v
```

Expected: tests pass with and without the local reranker model.

## Task 7: Evidence Filtering And Citation Diversity

**Files:**
- Modify: `core/retrieval.py`
- Modify: `core/knowledge_base.py`
- Modify: `tests/test_retrieval.py`

- [ ] **Step 1: Write failing tests**

Cover:

- final evidence keeps distinct source documents for multi-doc style queries when scores are close
- duplicate citations from the same parent are collapsed
- citation payload still excludes `source_path`
- score breakdown includes `rrf`, `lexical_rerank`, `semantic_rerank`, `graph`, `keyword`, `source`, and `final`

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_retrieval -v
```

Expected: FAIL until citation diversity is explicit.

- [ ] **Step 2: Implement citation diversity**

Add a final selection step after ranking and before `build_citations()`:

- prefer highest score overall
- avoid returning more than two evidence items from the same `source_filename` when other competitive sources exist
- keep deterministic ordering by final score and stable metadata

- [ ] **Step 3: Verify citation filtering**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_retrieval -v
```

Expected: tests pass and citation payload remains privacy-safe.

## Task 8: Candidate Evaluation Loop

**Files:**
- Modify if needed: `scripts/eval_rag.py`
- Run: `scripts/run-rag-eval.ps1`
- Run: `scripts/analyze_rag_eval_misses.py`

- [ ] **Step 1: Run candidate evaluation**

Run:

```powershell
.\scripts\run-rag-eval.ps1 -Workspace <SoulDrive workspace> -ResultPath eval/results/retrieval_candidate_v0.3.jsonl
```

- [ ] **Step 2: Compare against baseline**

Run:

```powershell
conda run -n souldrive python scripts/eval_rag.py --dataset eval/retrieval_quality_blind_v0.3.jsonl --compare-results eval/results/retrieval_baseline_v0.3.jsonl eval/results/retrieval_candidate_v0.3.jsonl --baseline-label baseline --candidate-label candidate
```

Expected:

```text
检索全命中率@3: baseline -> candidate
跨文档全命中率@3: baseline -> candidate
```

- [ ] **Step 3: Diagnose remaining misses**

Run:

```powershell
conda run -n souldrive python scripts/analyze_rag_eval_misses.py --dataset eval/retrieval_quality_blind_v0.3.jsonl --results eval/results/retrieval_candidate_v0.3.jsonl
```

Use this report to decide whether the next iteration belongs in this query-time branch or in a later ingestion-quality branch.

## Task 9: Final Regression

**Files:**
- Verify only

- [ ] **Step 1: Run targeted backend tests**

Run:

```powershell
conda run -n souldrive python -m unittest tests.test_query_expansion tests.test_retrieval tests.test_reranker tests.test_secure_vector_store tests.test_eval_assets tests.test_rag_eval_corpus_import tests.test_scripts -v
```

Expected: all tests pass.

- [ ] **Step 2: Run full backend suite**

Run:

```powershell
conda run -n souldrive python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 3: Run strict retrieval gate**

Run:

```powershell
.\scripts\run-rag-eval.ps1 -Workspace <SoulDrive workspace> -ResultPath eval/results/retrieval_candidate_v0.3.jsonl
```

Expected:

```text
检索全命中率@3: >= 80.0%
```

- [ ] **Step 4: Preserve review state**

Do not merge, rebase, cherry-pick, push, or commit automatically. Leave the worktree dirty for manual review unless the user explicitly requests staging or committing.
