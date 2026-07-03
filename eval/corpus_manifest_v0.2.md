# RAG Retrieval Quality Corpus Manifest v0.2

This manifest defines the document set for the `codex/rag-retrieval-quality` branch.
It is intentionally limited to query-time retrieval quality work: query rewrite,
retrieval fusion, rerank, citation filtering, and answer refusal checks.

## Scope

- Manifest file: `eval/corpus_manifest_v0.2.json`
- Existing local documents: 12
- Download candidates: 24
- Total planned documents: 36
- Planned strict evaluation samples: 360
- Sample size guardrail: 300-500

## Corpus Policy

The current branch can use this corpus to create a baseline before changing RAG
retrieval logic. The baseline should be saved before any query rewrite, recall
fusion, or rerank change is made.

Do not use this branch for ingestion-time experiments such as chunking strategy,
metadata schema, embedding model changes, or graph construction changes. Those
experiments require a separate branch and a freshly rebuilt index from the same
raw PDFs.

## Local Existing Documents

These files are already present in `C:\Users\13423\Desktop\souldrive_docs` and
are marked as `present` in the manifest:

1. LazyLLM企业级RAG方案-私有化部署与权限安全.pdf
2. “城市+AI”应用场景清单（第五批）.pdf
3. 人工智能安全治理蓝皮书.pdf
4. 基于大模型和RAG的知识库问答系统.pdf
5. 大模型基准测试体系研究报告.pdf
6. 大模型知识管理系统.pdf
7. 大模型私有部署与基础应用落地.pdf
8. 数据安全技术 数据分类分级规则.pdf
9. 数据要素发展报告.pdf
10. 生成式人工智能服务安全基本要求.pdf
11. 知识库问答场景中大语言模型私有化研究与应用实践.pdf
12. 综合交通运输大模型智能体创新应用.pdf

## Download Candidate Groups

The 24 download candidates are split across four evidence groups:

- LLM deployment and enterprise AI: SDOC-013 to SDOC-017
- AI safety, data security, and privacy standards: SDOC-018 to SDOC-024
- Data infrastructure and data element governance: SDOC-025 to SDOC-026
- RAG retrieval, rerank, benchmark, and query expansion papers: SDOC-027 to SDOC-036

Each document has a `question_budget` of 10, giving a planned 360-item strict
evaluation set. The final evaluation questions should include:

- Single-document factual retrieval questions.
- Cross-document comparison questions.
- Retrieval-only questions where all expected sources must appear in top 3.
- Rerank-sensitive questions with similar but wrong documents as distractors.
- Refusal questions for information not supported by the local corpus.
- Privacy and data security questions that require precise citation boundaries.

## Baseline Procedure

1. Download all `to_download` documents into the local corpus directory.
2. Start the desktop app from this worktree before changing RAG retrieval code.
3. Import all 36 documents.
4. Run the strict v0.2 evaluation set once.
5. Save the result as the branch baseline.
6. Only then change query rewrite, recall fusion, rerank, or citation filtering.

If later work changes document chunking, indexing, embeddings, metadata, or graph
construction, discard this query-time baseline for that experiment and rebuild a
fresh ingestion baseline in a new branch.
