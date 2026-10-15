from core.experiment_ledger import plan_experiment_material_review


def _table(text, **extra):
    return {"id": "E1", "content_kind": "table", "source_filename": "metrics.csv",
            "source_locator": "rows:2-3", "context": text, **extra}


def test_metric_material_review_reports_header_gaps_without_inferred_units_or_dataset():
    table = _table("| metric | value | | --- | --- | | p95_latency_ms | 900 |")
    config = {"id": "E2", "context": "dataset: other-data-v8", "source_filename": "config.yaml"}
    plan = plan_experiment_material_review("比较两轮指标和延迟变化", [table, config])
    assert plan["status"] == "needs_confirmation"
    assert set(plan["tables"][0]["missing_header_fields"]) == {
        "unit", "direction", "definition_version", "split", "dataset_version", "performed_at", "evaluation_script_ref"}
    assert plan["tables"][0]["evidence_id"] == "E1"
    assert plan["writes"] is False


def test_complete_headers_are_candidates_not_confirmed_measurements():
    header = "指标 | 数值 | 单位 | 方向 | 定义版本 | 划分 | 数据集版本 | 实验时间 | 评测脚本版本"
    table = _table(f"| {header} |\n| " + " | ".join(["---"] * 9) + " |")
    plan = plan_experiment_material_review("对比两次实验指标", [table])
    assert plan["tables"][0]["missing_header_fields"] == []
    assert plan["status"] == "needs_confirmation"
    assert plan["confirmed"] is False


def test_non_comparison_and_untrusted_material_do_not_trigger_metric_review():
    table = _table("| metric | value |\n| --- | --- |\n| score | 0.4 |")
    assert plan_experiment_material_review("这个指标记录了多少", [table]) is None
    assert plan_experiment_material_review("比较两份配置文件", [table]) is None
    for extra in ({"id": "M1"}, {"id": "E?"}, {"derived_index": True}, {"derived_index": "true"}):
        assert plan_experiment_material_review("比较实验指标", [_table(table["context"], **extra)]) is None
    assert plan_experiment_material_review("比较实验指标", [_table("采购记录，无指标表头。")]) is None
