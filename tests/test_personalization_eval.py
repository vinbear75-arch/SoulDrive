from pathlib import Path

import pytest

from scripts.eval_personalization import evaluate, load_records


def test_paired_personalization_eval_keeps_facts_and_citations_invariant():
    report = evaluate([
        {
            "schema_version": "souldrive_personalization_pair_v0.1",
            "pair_id": "PAIR-1",
            "question": "实验准确率是多少？",
            "evidence_ids": ["M1"],
            "baseline_answer": "准确率为 92%。 [M1]",
            "personalized_answer": "结论：准确率为 92%。 [M1]",
            "expected_facts": ["92%"],
            "expected_profile_markers": ["结论："],
            "forbidden_profile_markers": ["已证实原因"],
            "forbidden_scope_markers": ["project-b"],
        }
    ])

    assert report["summary"]["preference_adherence"] == 1.0
    assert report["summary"]["factual_invariance"] == 1.0
    assert report["summary"]["citation_invariance"] == 1.0
    assert report["summary"]["cross_project_leakage_rate"] == 0.0
    assert report["summary"]["pass_rate"] == 1.0


def test_paired_personalization_eval_fails_on_changed_citation_or_scope_leakage():
    report = evaluate([
        {
            "schema_version": "souldrive_personalization_pair_v0.1",
            "pair_id": "PAIR-2",
            "question": "项目数据是什么？",
            "evidence_ids": ["E1"],
            "baseline_answer": "项目 A 数据。 [E1]",
            "personalized_answer": "项目 B 数据。 [E2]",
            "expected_facts": ["项目 A"],
            "expected_profile_markers": [],
            "forbidden_profile_markers": [],
            "forbidden_scope_markers": ["项目 B"],
        }
    ])

    result = report["results"][0]
    assert result["citation_invariance"] is False
    assert result["cross_project_leakage"] is True
    assert report["summary"]["cross_project_leakage_rate"] == 1.0
    assert result["passed"] is False


def test_paired_personalization_eval_reports_partial_scope_leakage_rate():
    common = {
        "schema_version": "souldrive_personalization_pair_v0.1",
        "question": "项目数据是什么？",
        "evidence_ids": ["E1"],
        "baseline_answer": "项目 A 数据。 [E1]",
        "personalized_answer": "项目 A 数据。 [E1]",
        "expected_facts": ["项目 A"],
        "expected_profile_markers": [],
        "forbidden_profile_markers": [],
    }
    report = evaluate([
        {**common, "pair_id": "PAIR-3", "forbidden_scope_markers": ["项目 B"]},
        {**common, "pair_id": "PAIR-4", "forbidden_scope_markers": ["项目 A"]},
    ])

    assert report["results"][0]["cross_project_leakage"] is False
    assert report["results"][1]["cross_project_leakage"] is True
    assert report["summary"]["cross_project_leakage_rate"] == 0.5


def test_paired_personalization_eval_rejects_duplicate_ids():
    record = {
        "schema_version": "souldrive_personalization_pair_v0.1",
        "pair_id": "PAIR-1",
        "question": "q",
        "evidence_ids": ["E1"],
        "baseline_answer": "a [E1]",
        "personalized_answer": "a [E1]",
        "expected_facts": [],
        "expected_profile_markers": [],
        "forbidden_profile_markers": [],
        "forbidden_scope_markers": [],
    }

    with pytest.raises(ValueError, match="duplicate"):
        evaluate([record, dict(record)])


def test_personalization_seed_dataset_is_valid():
    records = load_records(Path("eval/personalization_pair_v0.1.jsonl"))
    assert len(records) == 2
    assert all(record["evidence_ids"] for record in records)
