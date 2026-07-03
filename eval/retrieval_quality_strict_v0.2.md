# Retrieval Quality Strict Evaluation v0.2

> Status: title-diagnostic only. This set contains broad full-title leakage and
> should not be used as the primary RAG retrieval-quality metric. Use
> `eval/retrieval_quality_blind_v0.3.jsonl` for the branch baseline and
> candidate comparisons. v0.2 remains useful only for checking the narrow
> known-title scenario where a user explicitly asks about a named document.

This dataset was originally created as the strict retrieval-quality gate for the
`codex/rag-retrieval-quality` branch. After audit, it is reclassified as a
known-title diagnostic set because most questions include full document titles.
It is built on the 36-document corpus in `eval/corpus_manifest_v0.2.json`.

## Files

- Dataset: `eval/retrieval_quality_strict_v0.2.jsonl`
- Corpus manifest: `eval/corpus_manifest_v0.2.json`
- Local file inventory: `eval/corpus_file_inventory_v0.2.json`
- Query-time optimization plan: `docs/superpowers/plans/2026-06-28-rag-retrieval-quality.md`

## Scale

- Total samples: 360
- Single-document retrieval: 144
- Safety, governance, privacy, or reliability retrieval: 72
- Cross-document retrieval: 108
- Refusal / unsupported-information checks: 36
- Covered document sources: 36 of 36

## Primary Metric

Use `retrieval_all_hit_rate@3` as the primary metric.

For positive samples, all expected sources must appear in the top 3 evidence
items. Cross-document samples are intentionally strict: a sample expecting two
documents only passes when both documents are retrieved within the top 3.

Secondary metrics:

- `retrieval_any_hit_rate@3`
- `first_hit_rate@1`
- `mean_reciprocal_rank`
- `multi_doc_all_hit_rate@3`
- `graph_context_hit_rate@3`
- refusal accuracy for negative samples
- citation validity when answer generation is enabled

## Baseline Workflow

1. Start the desktop app from this worktree before changing query-time RAG code,
   or use the import helper against an explicit workspace.
2. Import all 36 PDFs from the local corpus directory. The default corpus
   directory is `%USERPROFILE%\Desktop\souldrive_docs`; override it with
   `SOULDRIVE_EVAL_CORPUS_DIR` or `--corpus-dir`.

   ```powershell
   conda run -n souldrive python scripts/import_rag_eval_corpus.py --workspace <SoulDrive workspace>
   ```

   To only preview missing files without importing:

   ```powershell
   conda run -n souldrive python scripts/import_rag_eval_corpus.py --workspace <SoulDrive workspace> --dry-run
   ```

   For machine-readable output through `conda run`, use `--json-output`
   instead of `--json` because `conda run` has its own `--json` flag:

   ```powershell
   conda run -n souldrive python scripts/import_rag_eval_corpus.py --workspace <SoulDrive workspace> --dry-run --json-output
   ```

   To import and immediately run the existing indexer:

   ```powershell
   conda run -n souldrive python scripts/import_rag_eval_corpus.py --workspace <SoulDrive workspace> --index
   ```

   For retrieval-only baseline setup, skip graph extraction so indexing does
   not spend hours on graph construction that `scripts/eval_rag.py` does not
   use in retrieval-only mode:

   ```powershell
   conda run -n souldrive python scripts/import_rag_eval_corpus.py --workspace <SoulDrive workspace> --index --skip-graph
   ```

3. Check that the workspace contains the v0.2 corpus:

   ```powershell
   conda run -n souldrive python scripts/check_rag_eval_workspace.py --workspace <SoulDrive workspace>
   ```

   Machine-readable preflight output is available with:

   ```powershell
   conda run -n souldrive python scripts/check_rag_eval_workspace.py --workspace <SoulDrive workspace> --json-output
   ```

   If the workspace stores imported documents encrypted, pass the workspace
   password:

   ```powershell
   conda run -n souldrive python scripts/check_rag_eval_workspace.py --workspace <SoulDrive workspace> --passphrase <workspace password>
   ```

4. Run retrieval-only diagnostic evaluation:

   ```powershell
   .\scripts\run-rag-eval.ps1 -Workspace <SoulDrive workspace> -Dataset eval/retrieval_quality_strict_v0.2.jsonl -ResultPath eval/results/retrieval_title_diagnostic_v0.2.jsonl
   ```

   The runner default now targets the v0.3 blind set, so pass `-Dataset` and
   `-ResultPath` explicitly when rerunning this diagnostic set.

5. Save the baseline report before changing query rewrite, recall fusion,
   rerank, or citation filtering.
6. After each retrieval optimization, rerun the same dataset and compare against
   the baseline:

   ```powershell
   .\scripts\run-rag-eval.ps1 -Workspace <SoulDrive workspace> -Dataset eval/retrieval_quality_strict_v0.2.jsonl -ResultPath eval/results/retrieval_candidate_title_diagnostic_v0.2.jsonl

   conda run -n souldrive python scripts/eval_rag.py --dataset eval/retrieval_quality_strict_v0.2.jsonl --compare-results eval/results/retrieval_title_diagnostic_v0.2.jsonl eval/results/retrieval_candidate_title_diagnostic_v0.2.jsonl --baseline-label title-diagnostic-baseline --candidate-label title-diagnostic-candidate
   ```

7. Diagnose misses before changing query-time code:

   ```powershell
   conda run -n souldrive python scripts/analyze_rag_eval_misses.py --dataset eval/retrieval_quality_strict_v0.2.jsonl --results eval/results/retrieval_title_diagnostic_v0.2.jsonl
   ```

   For machine-readable output:

   ```powershell
   conda run -n souldrive python scripts/analyze_rag_eval_misses.py --dataset eval/retrieval_quality_strict_v0.2.jsonl --results eval/results/retrieval_title_diagnostic_v0.2.jsonl --json-output
   ```

## Scope Boundary

This dataset should not be used as proof for ingestion-time improvements unless
the index is rebuilt from the same 36 PDFs. Chunking strategy, metadata schema,
embedding model changes, and graph construction changes belong in a later
ingestion-quality branch with a fresh baseline.
