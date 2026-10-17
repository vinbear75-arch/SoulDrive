import json

import pytest

from core.answer_quality import validate_answer_citations
from core.experiment_ledger import build_experiment_evidence
from core.rag_engine import evidence_fallback_answer
from core.presentation_service import PresentationService


def _comparison(unit="%", baseline=40, current=44):
    return {
        "status": "comparable", "project_id": "synthetic",
        "compatibility": {"change_computation": "enabled"},
        "baseline": {"run_id": "before"}, "current": {"run_id": "after"},
        "changes": [{"metric": "quality_score", "unit": unit,
                     "baseline_value": baseline, "current_value": current,
                     "absolute_delta": current - baseline,
                     "relative_delta": None if not baseline else (current - baseline) / abs(baseline)}],
    }


def _evidence(unit="%", baseline=40, current=44):
    return build_experiment_evidence(_comparison(unit, baseline, current))


@pytest.mark.parametrize("unit", ["%", "％"])
@pytest.mark.parametrize("baseline,current,delta", [(40, 44, "+4"), (44, 40, "-4"), (40, 40, "0")])
def test_confirmed_percent_measurements_separate_point_delta_from_relative_percent(unit, baseline, current, delta):
    evidence = _evidence(unit, baseline, current)
    assert evidence[0]["unit"] == unit
    assert evidence[0]["absolute_delta"] == current - baseline
    assert evidence[0]["absolute_delta_unit"] == "个百分点"
    answer = f"quality_score 的绝对变化为 {delta} 个百分点。[M1]"
    assert validate_answer_citations(answer, evidence, semantic=True)["valid"]
    fallback = evidence_fallback_answer("报告 quality_score 变化", evidence)
    assert "个百分点" in fallback
    assert validate_answer_citations(fallback, evidence, semantic=True)["valid"]


@pytest.mark.parametrize("unit", ["", "ratio", "ms", "points"])
def test_non_percent_measurements_do_not_acquire_percentage_point_units(unit):
    evidence = _evidence(unit)
    assert evidence[0]["absolute_delta_unit"] == unit
    assert not validate_answer_citations("quality_score 的绝对变化为 4 个百分点。[M1]", evidence, semantic=True)["valid"]


@pytest.mark.parametrize("answer", [
    "quality_score 的绝对变化为 -4 个百分点。[M1]",
    "quality_score 的绝对变化为 5 个百分点。[M1]",
    "quality_score 的相对变化为 4%。[M1]",
    "quality_score 的相对变化为 10.01%。[M1]",
])
def test_percentage_point_support_does_not_authorize_changed_values_sign_or_relative_change(answer):
    assert not validate_answer_citations(answer, _evidence(), semantic=True)["valid"]


def test_plain_numeric_source_does_not_silently_supply_percentage_point_unit():
    assert not validate_answer_citations("quality_score 的变化为 4 个百分点。[E1]",
        [{"id": "E1", "context": "quality_score 的变化为 4。"}], semantic=True)["valid"]


def test_presentation_summary_uses_the_same_explicit_delta_unit():
    outline = PresentationService._with_experiment_summary({"slides": [{"title": "title"}, {"title": "end"}]},
        {"experiment_comparison": _comparison(), "source_filenames": ["synthetic.csv"], "max_slides": 4})
    text = json.dumps(outline, ensure_ascii=False)
    assert "40% → 44%" in text and "绝对变化 +4个百分点 [M1]" in text


@pytest.mark.parametrize("answer,baseline,current,valid", [
    ("quality_score 的基线值为40%，当前值为44%。[M1]", 40, 44, True),
    ("quality_score 的基线值为44%，当前值为40%。[M1]", 40, 44, False),
    ("quality_score 的相对变化为4%。[M1]", 0, 4, False),
    ("quality_score 的相对变化为10%。[M1]", 40, 44, True),
])
def test_typed_measurements_bind_numbers_to_roles_not_a_shared_number_bag(answer, baseline, current, valid):
    assert validate_answer_citations(answer, _evidence("%", baseline, current), semantic=True)["valid"] is valid


@pytest.mark.parametrize("reference,valid", [("env-cedar", True), ("env-ghost", False)])
def test_technical_reference_identifiers_must_exist_in_cited_sources(reference, valid):
    evidence = [*_evidence(), {"id": "E1", "context": "quality_score 的环境为 env-cedar；配置为 config-amber。"}]
    assert validate_answer_citations(f"quality_score 的环境为 {reference}。[E1][M1]", evidence, semantic=True)["valid"] is valid


def test_another_cited_metric_cannot_supply_the_named_metrics_baseline():
    other = _evidence("ms", 10, 20)[0]
    other = {**other, "id": "M2", "metric": "latency", "context": other["context"].replace("quality_score", "latency")}
    assert not validate_answer_citations("quality_score 的基线值为10ms。[M1][M2]", [*_evidence(), other], semantic=True)["valid"]


def test_time_reference_is_not_mistaken_for_the_current_numeric_measurement():
    evidence = [*_evidence(), {"id": "E1", "context": "quality_score 的记录时间为2026年。"}]
    assert validate_answer_citations("quality_score 当前2026年记录的值为44%。[E1][M1]", evidence, semantic=True)["valid"]


def test_explicit_bare_current_role_still_requires_the_current_value():
    assert not validate_answer_citations("quality_score 当前为40%。[M1]", _evidence(), semantic=True)["valid"]


@pytest.mark.parametrize("filename,valid", [("config-amber.pdf", True), ("config-ghost.pdf", False)])
def test_reference_guard_keeps_exact_cited_file_identity_without_inventing_other_files(filename, valid):
    evidence = [*_evidence(), {"id": "E1", "source_filename": "config-amber.pdf", "context": "quality_score 当前值为44%。"}]
    assert validate_answer_citations(f"quality_score 的记录来自 {filename}，当前值为44%。[E1][M1]", evidence, semantic=True)["valid"] is valid
