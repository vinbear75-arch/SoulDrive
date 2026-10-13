from pathlib import Path
import tempfile
from unittest.mock import patch

from core.calendar_export import export_ics, normalize_start_at
from core.workspace import SoulDriveWorkspace


def test_confirmed_schedule_exports_standard_ics_to_workspace_data():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        output = Path(export_ics(workspace, "artifact_demo", {
            "title": "组会", "start_at": "2026-08-24T10:00:00+08:00", "duration_minutes": 60,
            "notes": "汇报 RAG 结果", "participant": {"display_name": "李老师", "email": "li@example.test"},
        }))
        payload = output.read_bytes().decode("utf-8")

    assert output.name == "artifact_demo.ics"
    assert "BEGIN:VCALENDAR" in payload
    assert "SUMMARY:组会" in payload
    assert "MAILTO:li@example.test" in payload


def test_local_html_datetime_is_normalized_before_calendar_export():
    assert normalize_start_at("2026-08-24T10:00").startswith("2026-08-24T10:00:00")


def test_repeated_export_with_same_artifact_timestamp_is_byte_stable():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        plan = {
            "title": "稳定导出",
            "start_at": "2026-08-24T10:00:00+08:00",
            "duration_minutes": 60,
        }
        output = Path(export_ics(workspace, "artifact_stable", plan, stamp_at="2026-08-24T02:00:00+00:00"))
        first = output.read_bytes()
        output = Path(export_ics(workspace, "artifact_stable", plan, stamp_at="2026-08-24T02:00:00+00:00"))
        second = output.read_bytes()

    assert first == second


def test_long_utf8_text_is_folded_without_corrupting_or_injecting_lines():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        title = "研究会议" * 40
        notes = "第一行\r\n" + ("长描述内容" * 40)
        output = Path(export_ics(workspace, "artifact_folded", {
            "title": title,
            "start_at": "2026-08-24T10:00:00+08:00",
            "duration_minutes": 60,
            "notes": notes,
        }))
        payload = output.read_bytes().decode("utf-8")

    physical_lines = payload.split("\r\n")
    assert all(len(line.encode("utf-8")) <= 75 for line in physical_lines)
    assert any(line.startswith(" ") for line in physical_lines)

    unfolded: list[str] = []
    for line in physical_lines:
        if line.startswith(" ") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    assert f"SUMMARY:{title}" in unfolded
    assert "DESCRIPTION:第一行\\n" + ("长描述内容" * 40) in unfolded


def test_attendee_common_name_uses_quoted_parameter_value_for_reserved_marks():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        output = Path(export_ics(workspace, "artifact_attendee", {
            "title": "参数值",
            "start_at": "2026-08-24T10:00:00+08:00",
            "duration_minutes": 60,
            "participant": {"display_name": "李老师, 研究组", "email": "li@example.test"},
        }))
        payload = output.read_bytes().decode("utf-8")

    assert 'ATTENDEE;CN="李老师, 研究组":MAILTO:li@example.test' in payload


def test_attendee_common_name_with_double_quote_keeps_address_and_drops_invalid_parameter():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        output = Path(export_ics(workspace, "artifact_attendee_quote", {
            "title": "参数值",
            "start_at": "2026-08-24T10:00:00+08:00",
            "duration_minutes": 60,
            "participant": {"display_name": '李"老师', "email": "li@example.test"},
        }))
        payload = output.read_bytes().decode("utf-8")

    assert "ATTENDEE:MAILTO:li@example.test" in payload
    assert 'CN="李"老师' not in payload


def test_attendee_address_rejects_control_characters_instead_of_injecting_content_lines():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()

        try:
            export_ics(workspace, "artifact_attendee_control", {
                "title": "地址安全",
                "start_at": "2026-08-24T10:00:00+08:00",
                "duration_minutes": 60,
                "participant": {"display_name": "李老师", "email": "li@example.test\x00"},
            })
        except ValueError as exc:
            assert "participant email" in str(exc)
        else:
            raise AssertionError("control characters must not reach the iCalendar address")


def test_atomic_export_keeps_existing_file_when_flush_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        output = Path(workspace.calendars_path) / "artifact_atomic.ics"
        original = b"existing complete calendar\r\n"
        output.write_bytes(original)
        with patch("core.calendar_export.os.fsync", side_effect=OSError("disk full")):
            try:
                export_ics(workspace, "artifact_atomic", {
                    "title": "不会半写",
                    "start_at": "2026-08-24T10:00:00+08:00",
                    "duration_minutes": 30,
                })
            except OSError as exc:
                assert "disk full" in str(exc)
            else:
                raise AssertionError("flush failure must be visible to the caller")

        assert output.read_bytes() == original
        assert list(output.parent.glob(f".{output.name}.*.tmp")) == []


def test_atomic_export_keeps_existing_file_when_replace_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        output = Path(workspace.calendars_path) / "artifact_atomic_replace.ics"
        original = b"existing complete calendar\r\n"
        output.write_bytes(original)
        with patch("core.calendar_export.os.replace", side_effect=OSError("replace denied")):
            try:
                export_ics(workspace, "artifact_atomic_replace", {
                    "title": "不会覆盖",
                    "start_at": "2026-08-24T10:00:00+08:00",
                    "duration_minutes": 30,
                })
            except OSError as exc:
                assert "replace denied" in str(exc)
            else:
                raise AssertionError("replace failure must be visible to the caller")

        assert output.read_bytes() == original
        assert list(output.parent.glob(f".{output.name}.*.tmp")) == []
