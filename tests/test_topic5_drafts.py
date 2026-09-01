import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import mcp_server
from core.enterprise_security import SlidingWindowRateLimiter
from core.project_catalog import ProjectCatalog
from core.workspace import SoulDriveWorkspace
import core.runtime_state as runtime_state


def test_local_contact_resolution_and_artifact_confirmation():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            catalog.create_contact("李老师", "li@example.test", aliases=["老师"], project_ids=["demo"], contact_id="li")
            catalog.create_contact("李同学", "li-student@example.test", aliases=["李"], project_ids=["demo"], contact_id="student")

            assert catalog.resolve_contacts("老师", project_id="demo")[0]["contact_id"] == "li"
            assert len(catalog.resolve_contacts("李", project_id="demo")) == 2
            assert {item["email"] for item in catalog.list_contacts(project_id="demo")} == {"li@example.test", "li-student@example.test"}
            artifact_id = catalog.create_artifact(
                "demo",
                "email_draft",
                {"subject": "评审", "side_effect": "none"},
                source_version_ids=["version-1"],
                confirmation_record={"mode": "test"},
                change_summary="测试草稿的生成计划已记录。",
            )
            artifact = catalog.get_artifact(artifact_id)
            assert artifact["confirmation_status"] == "draft"
            assert artifact["confirmation_record"]["mode"] == "test"
            assert artifact["change_summary"] == "测试草稿的生成计划已记录。"
            confirmed = catalog.set_artifact_status(artifact_id, "confirmed")
            assert confirmed["confirmation_status"] == "confirmed"
            assert confirmed["confirmation_record"]["previous_status"] == "draft"
            updated = catalog.update_artifact_provenance(
                artifact_id,
                plan_patch={"output_path": "runtime/report.eml"},
                confirmation_record_patch={"delivery_status": "not_sent"},
            )
            assert updated["plan"]["output_path"] == "runtime/report.eml"
            assert updated["confirmation_record"]["delivery_status"] == "not_sent"
        finally:
            catalog.close()


def test_email_draft_requires_unique_local_contact_and_confirm_is_a_state_change(monkeypatch):
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            catalog.create_contact("李老师", "li@example.test", aliases=["老师"], project_ids=["demo"], contact_id="li")
            asset_id = catalog.create_asset("demo", "实验报告", "document", asset_id="report-asset")
            catalog.create_asset_version(asset_id, "report-hash", "report.pdf", version_id="report-version")
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.get_workspace_keys", return_value=SimpleNamespace(document_key=b"0" * 32, metadata_key=b"1" * 32)), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            created = client.post(
                "/drafts/email",
                headers=headers,
                json={
                    "project_id": "demo",
                    "recipient_query": "老师",
                    "subject": "实验评审",
                    "body": "请帮忙审阅实验报告。",
                    "attachment_refs": ["report.pdf"],
                },
            )
            ambiguous = client.post(
                "/drafts/email",
                headers=headers,
                json={
                    "project_id": "demo",
                    "recipient_query": "不存在的人",
                    "subject": "实验评审",
                    "body": "请帮忙审阅实验报告。",
                },
            )
            artifact_id = created.json()["artifact"]["artifact_id"]
            confirmed = client.post(
                f"/drafts/{artifact_id}/status",
                headers=headers,
                json={"status": "confirmed"},
            )
            repeated = client.post(
                f"/drafts/{artifact_id}/status",
                headers=headers,
                json={"status": "cancelled"},
            )

    assert created.status_code == 200
    assert created.json()["artifact"]["confirmation_status"] == "draft"
    assert created.json()["artifact"]["confirmation_record"]["status"] == "draft"
    assert "创建邮件草稿" in created.json()["artifact"]["change_summary"]
    assert created.json()["artifact"]["source_version_ids"] == ["report-version"]
    assert created.json()["artifact"]["plan"]["source_refs"][0]["status"] == "resolved"
    assert created.json()["artifact"]["plan"]["recipient"]["email"] == "li@example.test"
    assert ambiguous.status_code == 404
    assert confirmed.status_code == 200
    assert confirmed.json()["artifact"]["confirmation_status"] == "confirmed"
    assert repeated.status_code == 409


def test_email_attachment_upload_can_feed_email_draft():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            catalog.create_contact("李老师", "li@example.test", aliases=["老师"], project_ids=["demo"], contact_id="li")
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch(
            "core.mcp_server.get_workspace_keys",
            return_value=SimpleNamespace(document_key=b"0" * 32, metadata_key=b"1" * 32),
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            uploaded = client.post(
                "/drafts/email/attachments",
                headers=headers,
                data={"project_id": "demo"},
                files={"attachments": ("report.pdf", b"demo pdf bytes", "application/pdf")},
            )
            created = client.post(
                "/drafts/email",
                headers=headers,
                json={
                    "project_id": "demo",
                    "recipient_query": "老师",
                    "subject": "附件测试",
                    "body": "请查收附件。",
                    "attachment_refs": uploaded.json()["attachment_refs"],
                },
            )

    assert uploaded.status_code == 200
    assert uploaded.json()["attachment_refs"] == ["report.pdf"]
    assert created.status_code == 200
    assert created.json()["artifact"]["plan"]["attachment_refs"] == ["report.pdf"]


def test_contacts_endpoint_lists_project_contacts_for_the_history_picker():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            catalog.create_contact("Handsome_Vin", "2956584050@qq.com", aliases=["Vin"], project_ids=["demo"], contact_id="vin")
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            response = client.get("/contacts", params={"project_id": "demo"}, headers={"X-SoulDrive-Token": "draft-token"})

    assert response.status_code == 200
    assert response.json()["contacts"][0]["email"] == "2956584050@qq.com"


def test_confirmed_email_can_send_once_and_rejects_unconfirmed_drafts():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            draft_id = catalog.create_artifact("demo", "email_draft", {"recipient": {"email": "li@example.test"}, "subject": "评审", "body": "请审阅。", "attachment_refs": []})
            confirmed_id = catalog.create_artifact("demo", "email_draft", {"recipient": {"email": "li@example.test"}, "subject": "已确认", "body": "请审阅。", "attachment_refs": []}, confirmation_status="confirmed")
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch("core.mcp_server.current_workspace", return_value=workspace), patch("core.mcp_server.get_workspace_keys", return_value=object()), patch("core.mcp_server.SecureDocumentStore") as store_class, patch("core.mcp_server.send_confirmed_email", return_value={"provider": "qq", "attachment_count": 0}), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            rejected = client.post(f"/drafts/{draft_id}/send", headers=headers, json={"sender": "sender@example.test", "idempotency_key": "a" * 16})
            sent = client.post(f"/drafts/{confirmed_id}/send", headers=headers, json={"sender": "sender@example.test", "idempotency_key": "b" * 16})
            duplicate = client.post(f"/drafts/{confirmed_id}/send", headers=headers, json={"sender": "sender@example.test", "idempotency_key": "b" * 16})

    assert rejected.status_code == 409
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"
    assert duplicate.status_code == 200
    assert duplicate.json()["idempotent"] is True


def test_failed_email_delivery_can_be_retried_with_a_new_idempotency_key():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            artifact_id = catalog.create_artifact(
                "demo", "email_draft", {"recipient": {"email": "li@example.test"}, "subject": "评审", "body": "请审阅。"}, confirmation_status="confirmed"
            )
            assert catalog.reserve_email_delivery(artifact_id, "a" * 16) == "reserved"
            catalog.finish_email_delivery(artifact_id, "failed", {"error": "temporary"})
            assert catalog.reserve_email_delivery(artifact_id, "b" * 16) == "reserved"
        finally:
            catalog.close()


def test_schedule_draft_normalizes_html_datetime_for_ics_export():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch("core.mcp_server.current_workspace", return_value=workspace), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            created = client.post("/drafts/schedule", headers=headers, json={"project_id": "demo", "title": "组会", "start_at": "2026-08-24T10:00", "duration_minutes": 60})

    assert created.status_code == 200
    assert created.json()["artifact"]["confirmation_record"]["required"] is True
    assert "未写入外部日历" in created.json()["artifact"]["change_summary"]
    assert "T10:00:00" in created.json()["artifact"]["plan"]["start_at"]
    assert created.json()["artifact"]["plan"]["reminder_sound_enabled"] is True


def test_schedule_draft_can_be_edited_and_confirmed_schedule_returns_to_draft():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch("core.mcp_server.current_workspace", return_value=workspace), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            created = client.post(
                "/drafts/schedule",
                headers=headers,
                json={"project_id": "demo", "title": "原定日程", "start_at": "2026-09-01T10:52", "duration_minutes": 60},
            )
            artifact_id = created.json()["artifact"]["artifact_id"]
            confirmed = client.post(
                f"/drafts/{artifact_id}/status",
                headers=headers,
                json={"status": "confirmed"},
            )
            updated = client.patch(
                f"/drafts/{artifact_id}",
                headers=headers,
                json={"title": "修改后的日程", "start_at": "2026-09-01T11:30", "duration_minutes": 45, "notes": "更新说明", "reminder_sound_enabled": False},
            )

    assert created.status_code == 200
    assert confirmed.status_code == 200
    assert updated.status_code == 200
    assert updated.json()["artifact"]["confirmation_status"] == "draft"
    assert updated.json()["artifact"]["plan"]["title"] == "修改后的日程"
    assert "T11:30:00" in updated.json()["artifact"]["plan"]["start_at"]
    assert updated.json()["artifact"]["plan"]["duration_minutes"] == 45
    assert updated.json()["artifact"]["plan"]["notes"] == "更新说明"
    assert updated.json()["artifact"]["plan"]["reminder_sound_enabled"] is False
    assert updated.json()["artifact"]["confirmation_record"]["previous_confirmation_status"] == "confirmed"
