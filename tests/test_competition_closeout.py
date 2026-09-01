from pathlib import Path

from scripts.build_competition_closeout import CONFIG_AND_DATA_FILES, EVIDENCE_FILES, build_report, render_markdown


def test_closeout_report_tracks_missing_evidence_and_never_claims_competition_ready(tmp_path):
    existing_path = tmp_path / EVIDENCE_FILES[0][0]
    existing_path.parent.mkdir(parents=True)
    existing_path.write_text("roadmap", encoding="utf-8")

    report = build_report(tmp_path, created_at="2026-08-31T00:00:00+00:00")

    assert report["schema_version"] == "souldrive_competition_closeout_v0.1"
    assert report["competition_ready"] is False
    assert report["mvp_ready_for_demo"] is False
    assert {item["stage"] for item in report["roadmap_stage_audit"]} == {"S0", "S1", "S2", "S3", "S4", "S5", "S6"}
    assert any("Tauri" in item for item in report["verification_summary"])
    assert EVIDENCE_FILES[0][0] not in report["evidence_missing"]
    assert EVIDENCE_FILES[1][0] in report["evidence_missing"]
    assert any("外部未公开评测" in item for item in report["forbidden_claims"])
    assert len(report["config_and_data_hashes"]) == len(CONFIG_AND_DATA_FILES)


def test_closeout_markdown_contains_claim_boundaries(tmp_path):
    for relative, _label in EVIDENCE_FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("evidence", encoding="utf-8")

    report = build_report(tmp_path, created_at="2026-08-31T00:00:00+00:00")
    markdown = render_markdown(report)

    assert report["mvp_ready_for_demo"] is True
    assert "MVP 演示可用：是" in markdown
    assert "竞赛完整验收：否" in markdown
    assert "不得宣称" in markdown
    assert "路线图逐项核查" in markdown
    assert "配置与数据哈希" in markdown
    assert "已跑验证" in markdown
    assert "GraphRAG 生产业务验收" in markdown
