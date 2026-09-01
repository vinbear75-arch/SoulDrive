from pathlib import Path
import tempfile

from core.calendar_export import export_ics, normalize_start_at
from core.workspace import SoulDriveWorkspace


def test_confirmed_schedule_exports_standard_ics_to_workspace_data():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        output = Path(export_ics(workspace, "artifact_demo", {
            "title": "组会", "start_at": "2026-08-24T10:00:00+08:00", "duration_minutes": 60,
            "notes": "汇报 RAG 结果", "participant": {"display_name": "李老师", "email": "li@example.test"},
        }))
        payload = output.read_text(encoding="utf-8")

    assert output.name == "artifact_demo.ics"
    assert "BEGIN:VCALENDAR" in payload
    assert "SUMMARY:组会" in payload
    assert "MAILTO:li@example.test" in payload


def test_local_html_datetime_is_normalized_before_calendar_export():
    assert normalize_start_at("2026-08-24T10:00").startswith("2026-08-24T10:00:00")
