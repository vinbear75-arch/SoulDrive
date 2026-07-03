# Retrieval Quality Blind Evaluation v0.3

This is the primary retrieval-quality gate for the
`codex/rag-retrieval-quality` branch. It uses the same 36-document corpus from
`eval/corpus_manifest_v0.2.json`, but fixes the v0.2 title-leakage problem by
making most questions blind to full document titles and filenames.

## Files

- Dataset: `eval/retrieval_quality_blind_v0.3.jsonl`
- Corpus manifest: `eval/corpus_manifest_v0.2.json`
- Previous title-diagnostic set: `eval/retrieval_quality_strict_v0.2.jsonl`
- Runner: `scripts/run-rag-eval.ps1`
- Miss analyzer: `scripts/analyze_rag_eval_misses.py`

## Scale

- Total samples: 360
- Blind samples: 300
- Known-title samples: 60
- Single-document retrieval: 144
- Safety, governance, privacy, or reliability retrieval: 72
- Cross-document retrieval: 108
- Refusal / unsupported-information checks: 36
- Covered document sources: 36 of 36

## Title Reference Policy

Each record has `allows_title_reference`.

- `false`: the question must not contain the full document title, full filename,
  or a complete quoted title for its target sources.
- `true`: the question may ask by full document title, e.g. "please summarize
  this named document." This keeps a small but realistic known-title retrieval
  capability check.

Domain terms from titles are allowed in blind questions. Examples include
`RAG`, `GraphRAG`, `HyDE`, `RankRAG`, `AI`, `data classification`, `privacy`,
`knowledge graph`, and similar technical terms. The blocked pattern is the full
source title or full filename, not ordinary technical vocabulary.

## Primary Metric

Use `retrieval_all_hit_rate@3` as the primary metric on the full v0.3 set, and
also report the blind-only subset separately when doing serious comparisons.

For positive samples, all expected sources must appear in the top 3 evidence
items. Cross-document samples are intentionally strict: a sample expecting two
documents only passes when both documents are retrieved within the top 3.

Secondary metrics:

- `retrieval_any_hit_rate@3`
- `first_hit_rate@1`
- `mean_reciprocal_rank`
- `average_source_coverage@3`
- `multi_doc_all_hit_rate@3`
- `graph_context_hit_rate@3`
- refusal accuracy for negative samples
- citation validity when answer generation is enabled

## Baseline Workflow

Import and index the same 36 PDFs from `eval/corpus_manifest_v0.2.json`, then run:

```powershell
.\scripts\run-rag-eval.ps1 -Workspace <SoulDrive workspace> -ResultPath eval/results/retrieval_baseline_v0.3.jsonl
```

After a candidate retrieval change:

```powershell
.\scripts\run-rag-eval.ps1 -Workspace <SoulDrive workspace> -ResultPath eval/results/retrieval_candidate_v0.3.jsonl

conda run -n souldrive python scripts/eval_rag.py --dataset eval/retrieval_quality_blind_v0.3.jsonl --compare-results eval/results/retrieval_baseline_v0.3.jsonl eval/results/retrieval_candidate_v0.3.jsonl --baseline-label baseline --candidate-label candidate
```

Diagnose misses with:

```powershell
conda run -n souldrive python scripts/analyze_rag_eval_misses.py --dataset eval/retrieval_quality_blind_v0.3.jsonl --results eval/results/retrieval_baseline_v0.3.jsonl
```

## Scope Boundary

This dataset is still for query-time retrieval work: query rewrite, recall
fusion, rerank, source diversity, and citation filtering. Chunking strategy,
metadata schema, embedding model changes, and graph construction changes belong
in a separate ingestion-quality branch with a fresh baseline.
