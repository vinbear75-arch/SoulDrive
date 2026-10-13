import pytest

from core.rag_engine import _validate_generated_answer_for_workflow, evidence_fallback_answer


EVIDENCE = [{
    "id": "M1", "evidence_type": "experiment_measurement", "section": "实验测量：accuracy",
    "metric": "accuracy", "unit": "%", "baseline_value": 0.81, "current_value": 0.86, "absolute_delta": 0.05,
    "context": "指标=accuracy；基线值=0.81%；当前值=0.86%；绝对变化=+0.05%；这些候选因素不能单独证明因果。",
}]


def answer_with_marker(marker):
    return ("## 结论\naccuracy 基线 0.81%，当前 0.86%，绝对变化 +0.05%。[M1]\n\n"
            "## 已证实变化\naccuracy 绝对变化 +0.05%。[M1]\n\n"
            f"## 高可信候选原因\n{marker}\n\n## 待验证假设\n待确认\n\n## 最小验证实验\n待确认")


def test_exact_missing_evidence_marker_is_not_an_uncited_fact():
    report = _validate_generated_answer_for_workflow(answer_with_marker("待确认"), EVIDENCE, "experiment_compare")
    assert report["valid"], report


@pytest.mark.parametrize("marker", ["待确认，但 accuracy 已提升 90%", "待确认：accuracy 为 90%", "待确认\naccuracy 提升 90%", "待确认，但 accuracy 已提升 90% [M1]"])
def test_missing_marker_cannot_hide_an_unsupported_claim(marker):
    report = _validate_generated_answer_for_workflow(answer_with_marker(marker), EVIDENCE, "experiment_compare")
    assert not report["valid"]


def test_measurement_fallback_satisfies_the_same_fact_and_workflow_gates():
    answer = evidence_fallback_answer("报告已确认测量", EVIDENCE, prefer_primary=True)
    report = _validate_generated_answer_for_workflow(answer, EVIDENCE, "experiment_compare")
    assert report["valid"], report


def test_measurement_fallback_preserves_ledger_display_precision():
    precise = {**EVIDENCE[0], "baseline_value": 0.123456789, "current_value": 0.223456789, "absolute_delta": 0.1,
               "context": "指标=accuracy；基线值=0.123456789%；当前值=0.223456789%；绝对变化=+0.1%；这些候选因素不能单独证明因果。"}
    answer = evidence_fallback_answer("报告已确认测量", [precise], prefer_primary=True)
    assert "0.123456789%" in answer and "0.223456789%" in answer
    assert _validate_generated_answer_for_workflow(answer, [precise], "experiment_compare")["valid"]
