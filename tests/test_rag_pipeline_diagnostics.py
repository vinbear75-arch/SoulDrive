import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from core.retrieval import SearchCandidate
from scripts import diagnose_rag_pipeline


class RagPipelineDiagnosticsTests(unittest.TestCase):
    def test_secure_stage_candidates_are_hydrated_before_source_evaluation(self):
        candidate = SearchCandidate(id="chunk-1", content="", metadata={})
        hydrated = SearchCandidate(
            id="chunk-1",
            content="正文",
            metadata={"source_filename": "source.pdf"},
        )
        kb = Mock()
        kb._hydrate_secure_candidates.return_value = [hydrated]

        result = diagnose_rag_pipeline.hydrate_stage_candidates(kb, [candidate], limit=10)

        self.assertEqual(result[0].metadata["source_filename"], "source.pdf")
        kb._hydrate_secure_candidates.assert_called_once_with([candidate], 10)

    def test_stage_metrics_report_any_all_and_coverage(self):
        samples = [
            _sample(
                "Q1",
                ["alpha.pdf", "beta.pdf"],
                {
                    "dense": ["alpha.pdf", "noise.pdf"],
                    "final": ["alpha.pdf", "beta.pdf"],
                },
            ),
            _sample(
                "Q2",
                ["gamma.pdf"],
                {
                    "dense": ["noise.pdf"],
                    "final": ["noise.pdf"],
                },
            ),
        ]

        summary = diagnose_rag_pipeline.summarize_diagnostics(samples, stage_order=["dense", "final"])

        dense = summary["stage_summary"]["dense"]
        self.assertEqual(dense["sample_count"], 2)
        self.assertEqual(dense["miss_count"], 2)
        self.assertEqual(dense["any_hit_rate"], 0.5)
        self.assertEqual(dense["all_hit_rate"], 0.0)
        self.assertEqual(dense["average_source_coverage"], 0.25)
        self.assertEqual(dense["avg_candidate_count"], 1.5)

        final = summary["stage_summary"]["final"]
        self.assertEqual(final["any_hit_rate"], 0.5)
        self.assertEqual(final["all_hit_rate"], 0.5)
        self.assertEqual(final["average_source_coverage"], 0.5)

    def test_failure_bucket_identifies_candidate_recall_gap(self):
        sample = _sample(
            "Q1",
            ["alpha.pdf"],
            {
                "dense": ["noise.pdf"],
                "keyword": ["noise.pdf"],
                "source_title": [],
                "merged": ["noise.pdf"],
                "reranked_pool": ["noise.pdf"],
                "final": ["noise.pdf"],
            },
        )

        buckets = diagnose_rag_pipeline.classify_failure_buckets(sample)

        self.assertIn("no_candidate_recall", buckets)
        self.assertIn("dense_recall_gap", buckets)
        self.assertIn("keyword_recall_gap", buckets)
        self.assertIn("blind_query_miss", buckets)

    def test_failure_bucket_identifies_rerank_loss(self):
        sample = _sample(
            "Q1",
            ["alpha.pdf"],
            {
                "dense": ["alpha.pdf"],
                "keyword": [],
                "source_title": [],
                "merged": ["alpha.pdf", "noise.pdf"],
                "reranked_pool": ["noise.pdf"],
                "final": ["noise.pdf"],
            },
        )

        buckets = diagnose_rag_pipeline.classify_failure_buckets(sample)

        self.assertIn("rerank_loss", buckets)
        self.assertNotIn("no_candidate_recall", buckets)

    def test_failure_bucket_identifies_topk_loss_and_partial_multi_doc(self):
        sample = _sample(
            "Q1",
            ["alpha.pdf", "beta.pdf"],
            {
                "dense": ["alpha.pdf", "beta.pdf"],
                "keyword": [],
                "source_title": [],
                "merged": ["alpha.pdf", "beta.pdf"],
                "reranked_pool": ["alpha.pdf", "beta.pdf"],
                "final": ["alpha.pdf"],
            },
            requires_multi_doc=True,
        )

        buckets = diagnose_rag_pipeline.classify_failure_buckets(sample)

        self.assertIn("topk_loss", buckets)
        self.assertIn("partial_multi_doc", buckets)

    def test_json_report_is_saved_with_stage_summary(self):
        samples = [
            _sample(
                "Q1",
                ["alpha.pdf"],
                {
                    "dense": ["alpha.pdf"],
                    "final": ["alpha.pdf"],
                },
                allows_title_reference=True,
            )
        ]
        report = diagnose_rag_pipeline.build_report(
            dataset_path=Path("eval/retrieval_quality_blind_v0.3.jsonl"),
            workspace_path=Path("E:/SoulDrive"),
            top_k=3,
            samples=samples,
            stage_order=["dense", "final"],
            graph_enabled=False,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "diagnostics.json"
            diagnose_rag_pipeline.save_report_json(report, output)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["sample_count"], 1)
        self.assertEqual(payload["positive_sample_count"], 1)
        self.assertFalse(payload["graph_enabled"])
        self.assertIn("dense", payload["stage_summary"])
        self.assertEqual(payload["samples"][0]["failure_buckets"], ["final_hit", "known_title_hit"])

    def test_compare_reports_tracks_stage_metric_and_bucket_deltas(self):
        baseline = {
            "stage_order": ["dense", "final"],
            "stage_summary": {
                "dense": {"any_hit_rate": 0.4, "all_hit_rate": 0.3, "average_source_coverage": 0.35},
                "final": {"any_hit_rate": 0.3, "all_hit_rate": 0.2, "average_source_coverage": 0.25},
            },
            "failure_bucket_counts": {"no_candidate_recall": 10, "topk_loss": 2},
        }
        candidate = {
            "stage_order": ["dense", "final"],
            "stage_summary": {
                "dense": {"any_hit_rate": 0.5, "all_hit_rate": 0.4, "average_source_coverage": 0.45},
                "final": {"any_hit_rate": 0.45, "all_hit_rate": 0.35, "average_source_coverage": 0.4},
            },
            "failure_bucket_counts": {"no_candidate_recall": 7, "topk_loss": 5},
        }

        comparison = diagnose_rag_pipeline.compare_reports(baseline, candidate)

        self.assertEqual(comparison["stage_deltas"]["final"]["all_hit_rate_delta"], 0.15)
        self.assertEqual(comparison["stage_deltas"]["dense"]["any_hit_rate_delta"], 0.1)
        self.assertEqual(comparison["failure_bucket_deltas"]["no_candidate_recall"], -3)
        self.assertEqual(comparison["failure_bucket_deltas"]["topk_loss"], 3)

    def test_stage_depth_scan_reports_recall_at_configured_depths(self):
        scan = diagnose_rag_pipeline.build_stage_depth_scan(
            expected_sources=["alpha.pdf"],
            candidate_groups=[
                _candidates(["noise-1.pdf", "noise-2.pdf", "alpha.pdf"]),
            ],
            depths=[1, 3],
        )

        self.assertFalse(scan["1"]["any_hit"])
        self.assertEqual(scan["1"]["source_coverage"], 0.0)
        self.assertTrue(scan["3"]["any_hit"])
        self.assertEqual(scan["3"]["source_coverage"], 1.0)

    def test_stage_identity_evaluation_rejects_legacy_source_and_keeps_runtime_id(self):
        result = diagnose_rag_pipeline.build_stage_result(
            expected_sources=["安全治理标准"],
            candidates=[
                {
                    "candidate_id": "candidate-old",
                    "source_filename": "安全治理标准-旧版.pdf",
                    "document_id": "asset-runtime-old",
                }
            ],
            expected_document_ids=["SDOC-001"],
        )

        self.assertFalse(result["any_hit"])
        self.assertEqual(result["missing_sources"], ["安全治理标准"])
        self.assertEqual(result["top_document_ids"], ["asset-runtime-old"])

    def test_stage_identity_evaluation_accepts_exact_source_with_runtime_id(self):
        result = diagnose_rag_pipeline.build_stage_result(
            expected_sources=["安全治理标准"],
            candidates=[
                {
                    "candidate_id": "candidate-current",
                    "source_filename": "安全治理标准.pdf",
                    "document_id": "asset-runtime-current",
                }
            ],
            expected_document_ids=["SDOC-001"],
        )

        self.assertTrue(result["any_hit"])
        self.assertEqual(result["hit_sources"], ["安全治理标准"])

    def test_report_summarizes_recall_depth_and_final_k_scans(self):
        sample = _sample(
            "Q1",
            ["alpha.pdf"],
            {
                "dense": ["noise.pdf"],
                "keyword": ["noise.pdf"],
                "merged": ["noise.pdf", "alpha.pdf"],
                "reranked_pool": ["noise.pdf", "alpha.pdf"],
                "final": ["noise.pdf"],
            },
        )
        sample["recall_depth_scan"] = {
            "dense": {
                "1": diagnose_rag_pipeline.build_stage_result(["alpha.pdf"], _candidates(["noise.pdf"])),
                "2": diagnose_rag_pipeline.build_stage_result(["alpha.pdf"], _candidates(["noise.pdf", "alpha.pdf"])),
            }
        }
        sample["final_k_scan"] = {
            "3": diagnose_rag_pipeline.build_stage_result(["alpha.pdf"], _candidates(["noise.pdf"])),
            "5": diagnose_rag_pipeline.build_stage_result(["alpha.pdf"], _candidates(["noise.pdf", "alpha.pdf"])),
        }

        report = diagnose_rag_pipeline.build_report(
            dataset_path=Path("eval/retrieval_quality_blind_v0.3.jsonl"),
            workspace_path=Path("E:/SoulDrive"),
            top_k=3,
            samples=[sample],
            stage_order=["dense", "keyword", "merged", "reranked_pool", "final"],
            graph_enabled=False,
        )

        self.assertEqual(report["recall_depth_summary"]["dense"]["1"]["any_hit_rate"], 0.0)
        self.assertEqual(report["recall_depth_summary"]["dense"]["2"]["any_hit_rate"], 1.0)
        self.assertEqual(report["final_k_summary"]["3"]["any_hit_rate"], 0.0)
        self.assertEqual(report["final_k_summary"]["5"]["any_hit_rate"], 1.0)


def _sample(
    record_id,
    expected_sources,
    stage_sources,
    requires_multi_doc=False,
    graph_relevant=False,
    allows_title_reference=False,
):
    stages = {}
    for name, sources in stage_sources.items():
        stages[name] = diagnose_rag_pipeline.build_stage_result(expected_sources, _candidates(sources))
    sample = {
        "id": record_id,
        "question": f"Question {record_id}",
        "type": "unit",
        "expected_sources": expected_sources,
        "requires_multi_doc": requires_multi_doc,
        "graph_relevant": graph_relevant,
        "allows_title_reference": allows_title_reference,
        "query_variants": [f"Question {record_id}"],
        "stages": stages,
        "elapsed_ms": 1,
    }
    sample["failure_buckets"] = diagnose_rag_pipeline.classify_failure_buckets(sample)
    return sample


def _candidates(sources):
    return [
        {
            "candidate_id": f"candidate-{index}",
            "source_filename": source,
            "parent_id": f"parent-{index}",
        }
        for index, source in enumerate(sources, start=1)
    ]


if __name__ == "__main__":
    unittest.main()
