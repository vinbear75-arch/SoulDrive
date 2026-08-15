import unittest
from pathlib import Path

from core.evaluation import (
    evaluate_retrieval_cases,
    load_retrieval_eval_cases,
    wilson_interval,
)
from scripts.build_structured_retrieval_eval_dataset import (
    CATEGORIES,
    build_records,
    dataset_summary,
)


class EvaluationTests(unittest.TestCase):
    def test_retrieval_eval_fixture_scores_expected_hits(self):
        fixture = Path(__file__).parent / "fixtures" / "retrieval_eval.jsonl"

        cases = load_retrieval_eval_cases(fixture)
        report = evaluate_retrieval_cases(cases, top_k=2)

        self.assertEqual(report["case_count"], 2)
        self.assertEqual(report["hit_rate_at_k"], 1.0)
        self.assertGreaterEqual(report["mean_reciprocal_rank"], 0.5)
        self.assertIn("evidence_acceptance_rate", report)
        self.assertIn("average_top_score", report)
        self.assertEqual(report["low_evidence_cases"], [])
        self.assertEqual(
            report["confidence_intervals_95"]["hit_rate_at_k"]["total"],
            2,
        )

    def test_wilson_interval_keeps_perfect_sample_below_absolute_certainty(self):
        interval = wilson_interval(360, 360)

        self.assertGreater(interval["low"], 0.98)
        self.assertLess(interval["low"], 1.0)
        self.assertEqual(interval["high"], 1.0)
        self.assertEqual(interval["successes"], 360)
        self.assertEqual(interval["total"], 360)

    def test_structured_retrieval_dataset_is_broad_and_stratified(self):
        records = build_records()
        summary = dataset_summary(records)

        self.assertEqual(summary["case_count"], 360)
        self.assertEqual(set(summary["categories"]), set(CATEGORIES))
        self.assertTrue(all(count == 60 for count in summary["categories"].values()))
        self.assertGreaterEqual(summary["difficulties"]["hard"], 180)
        self.assertEqual(summary["candidate_count"], 3600)
        self.assertTrue(all(len(record["candidates"]) == 10 for record in records))
        self.assertTrue(all(record["expected_ids"] for record in records))
        grouped = [record for record in records if record.get("expected_groups")]
        self.assertEqual(len(grouped), 120)
        self.assertTrue(all(len(record["expected_groups"]) == 2 for record in grouped))

    def test_retrieval_report_includes_category_and_recall_metrics(self):
        records = build_records(cases_per_category=2)
        fixture_path = Path(__file__).parent / "fixtures" / "retrieval_eval.jsonl"
        fixture_cases = load_retrieval_eval_cases(fixture_path)
        self.assertTrue(fixture_cases)

        import json
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            generated_path = Path(temp_dir) / "structured.jsonl"
            generated_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
                encoding="utf-8",
            )
            cases = load_retrieval_eval_cases(generated_path)
            report = evaluate_retrieval_cases(cases, top_k=3)

        self.assertEqual(report["case_count"], 12)
        self.assertEqual(set(report["category_metrics"]), set(CATEGORIES))
        self.assertIn("all_hit_rate_at_k", report)
        self.assertIn("mean_expected_recall_at_k", report)
        self.assertIn("dense_baseline_all_hit_rate_at_k", report)
        self.assertGreater(
            report["all_hit_rate_at_k"],
            report["dense_baseline_all_hit_rate_at_k"],
        )
        self.assertEqual(
            sum(item["case_count"] for item in report["category_metrics"].values()),
            12,
        )


if __name__ == "__main__":
    unittest.main()
