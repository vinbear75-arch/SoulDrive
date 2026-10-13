import json
import threading
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import MagicMock

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
                params={"project_id": "demo"},
                headers=headers,
                json={"status": "confirmed"},
            )
            repeated = client.post(
                f"/drafts/{artifact_id}/status",
                params={"project_id": "demo"},
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


def test_email_draft_idempotency_key_replays_same_artifact_and_rejects_changed_input():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            catalog.create_contact(
                "李老师",
                "li@example.test",
                aliases=["老师"],
                project_ids=["demo"],
                contact_id="li",
            )
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            payload = {
                "project_id": "demo",
                "recipient_query": "老师",
                "subject": "幂等草稿",
                "body": "同一个请求不能创建两份草稿。",
                "idempotency_key": "email-draft-retry-001",
            }
            first = client.post("/drafts/email", headers=headers, json=payload)
            replay = client.post("/drafts/email", headers=headers, json=payload)
            changed = client.post(
                "/drafts/email",
                headers=headers,
                json={**payload, "subject": "同键换内容"},
            )
            artifacts = client.get("/projects/demo/artifacts", headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["artifact"]["artifact_id"] == first.json()["artifact"]["artifact_id"]
    assert changed.status_code == 400
    assert "idempotency_key" in changed.json()["error"]
    assert len(artifacts.json()["artifacts"]) == 1


def test_email_draft_idempotent_replay_does_not_duplicate_created_audit_event():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            catalog.create_contact("李老师", "li@example.test", aliases=["老师"], project_ids=["demo"], contact_id="li")
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        events = []
        audit = SimpleNamespace(append_event=lambda event_type, payload=None: events.append((event_type, payload)))
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            payload = {
                "project_id": "demo",
                "recipient_query": "老师",
                "subject": "审计幂等",
                "body": "同一个草稿重试不应重复写创建事件。",
                "idempotency_key": "email-audit-replay-001",
            }
            first = client.post("/drafts/email", headers=headers, json=payload)
            replay = client.post("/drafts/email", headers=headers, json=payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert [event_type for event_type, _ in events] == ["artifact.email_draft_created"]


def test_email_draft_create_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("邮件草稿审计", project_id="demo")
            catalog.create_contact("审计联系人", "audit-recipient@example.test", project_ids=["demo"], contact_id="audit-recipient")
        finally:
            catalog.close()
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/drafts/email",
                headers={"X-SoulDrive-Token": "draft-token"},
                json={
                    "project_id": "demo",
                    "recipient_query": "审计联系人",
                    "subject": "审计边界",
                    "body": "创建成功后仍应返回草稿。",
                    "idempotency_key": "email-audit-create-001",
                },
            )

    assert response.status_code == 200
    assert response.json()["artifact"]["artifact_type"] == "email_draft"
    assert "审计记录写入失败" in response.json()["warning"]


def test_schedule_draft_idempotent_replay_does_not_duplicate_created_audit_event():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        events = []
        audit = SimpleNamespace(append_event=lambda event_type, payload=None: events.append((event_type, payload)))
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            payload = {
                "title": "审计幂等日程",
                "start_at": "2026-09-20T10:00",
                "duration_minutes": 30,
                "idempotency_key": "schedule-audit-replay-001",
            }
            first = client.post("/drafts/schedule", headers=headers, json=payload)
            replay = client.post("/drafts/schedule", headers=headers, json=payload)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert [event_type for event_type, _ in events] == ["artifact.schedule_draft_created"]


def test_schedule_draft_create_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/drafts/schedule",
                headers={"X-SoulDrive-Token": "draft-token"},
                json={
                    "title": "审计故障日程",
                    "start_at": "2026-09-20T10:00",
                    "duration_minutes": 30,
                    "idempotency_key": "schedule-audit-create-001",
                },
            )

    assert response.status_code == 200
    assert response.json()["artifact"]["artifact_type"] == "schedule_draft"
    assert "审计记录写入失败" in response.json()["warning"]


def test_schedule_draft_update_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            artifact_id = catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "原日程", "start_at": "2026-09-20T10:00:00+00:00", "duration_minutes": 30},
            )
        finally:
            catalog.close()
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).patch(
                f"/drafts/{artifact_id}",
                headers={"X-SoulDrive-Token": "draft-token"},
                json={"title": "修改后日程", "start_at": "2026-09-20T11:00", "duration_minutes": 45},
            )

    assert response.status_code == 200
    assert response.json()["artifact"]["plan"]["title"] == "修改后日程"
    assert "审计记录写入失败" in response.json()["warning"]


def test_schedule_draft_delete_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            artifact_id = catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "待删除审计日程", "start_at": "2026-09-20T10:00:00+00:00", "duration_minutes": 30},
            )
        finally:
            catalog.close()
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("LOCAL", None, None, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).delete(
                f"/drafts/{artifact_id}", headers={"X-SoulDrive-Token": "draft-token"}
            )

    assert response.status_code == 200
    assert response.json()["artifact"]["confirmation_status"] == "cancelled"
    assert "审计记录写入失败" in response.json()["warning"]
def test_project_bound_email_draft_requires_matching_scope_for_read_and_mutation():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("项目 A", project_id="project-a")
            catalog.create_project("项目 B", project_id="project-b")
            catalog.create_contact(
                "项目 A 联系人",
                "project-a@example.test",
                aliases=["项目 A"],
                project_ids=["project-a"],
                contact_id="project-a-contact",
            )
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch(
            "core.mcp_server.get_workspace_keys",
            return_value=SimpleNamespace(document_key=b"0" * 32, metadata_key=b"1" * 32),
        ), patch(
            "core.mcp_server.send_confirmed_email",
            return_value={"provider": "qq", "attachment_count": 0},
        ) as send_email, patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            created = client.post(
                "/drafts/email",
                headers=headers,
                json={
                    "project_id": "project-a",
                    "recipient_query": "项目 A",
                    "subject": "项目范围测试",
                    "body": "只允许项目 A 使用。",
                },
            )
            assert created.status_code == 200, created.json()
            artifact_id = created.json()["artifact"]["artifact_id"]
            missing_scope = client.get(f"/drafts/{artifact_id}", headers=headers)
            foreign_scope = client.get(
                f"/drafts/{artifact_id}",
                params={"project_id": "project-b"},
                headers=headers,
            )
            correct_scope = client.get(
                f"/drafts/{artifact_id}",
                params={"project_id": "project-a"},
                headers=headers,
            )
            missing_status_scope = client.post(
                f"/drafts/{artifact_id}/status",
                headers=headers,
                json={"status": "confirmed"},
            )
            foreign_status_scope = client.post(
                f"/drafts/{artifact_id}/status",
                params={"project_id": "project-b"},
                headers=headers,
                json={"status": "confirmed"},
            )
            confirmed = client.post(
                f"/drafts/{artifact_id}/status",
                params={"project_id": "project-a"},
                headers=headers,
                json={"status": "confirmed"},
            )
            foreign_send_scope = client.post(
                f"/drafts/{artifact_id}/send",
                params={"project_id": "project-b"},
                headers=headers,
                json={"sender": "sender@example.test", "idempotency_key": "s" * 16},
            )

    assert created.status_code == 200
    assert missing_scope.status_code == 409
    assert foreign_scope.status_code == 409
    assert correct_scope.status_code == 200
    assert missing_status_scope.status_code == 409
    assert foreign_status_scope.status_code == 409
    assert confirmed.status_code == 200
    assert foreign_send_scope.status_code == 409
    send_email.assert_not_called()


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


def test_email_attachment_binding_rejects_same_name_from_another_project():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            for project_id in ("project-a", "project-b"):
                catalog.create_project(project_id, project_id=project_id)
                catalog.create_contact(
                    f"{project_id} 联系人",
                    f"{project_id}@example.test",
                    aliases=[project_id],
                    project_ids=[project_id],
                    contact_id=f"contact-{project_id}",
                )
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
                data={"project_id": "project-a"},
                files={"attachments": ("report.pdf", b"project-a bytes", "application/pdf")},
            )
            foreign_draft = client.post(
                "/drafts/email",
                headers=headers,
                json={
                    "project_id": "project-b",
                    "recipient_query": "project-b",
                    "subject": "跨项目附件",
                    "body": "不得带出项目 A 的附件。",
                    "attachment_refs": uploaded.json()["attachment_refs"],
                },
            )

    assert uploaded.status_code == 200
    assert foreign_draft.status_code == 409
    assert "project" in foreign_draft.json()["error"]


def test_email_send_reads_the_project_bound_object_for_duplicate_filenames():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            for project_id in ("project-a", "project-b"):
                catalog.create_project(project_id, project_id=project_id)
                catalog.create_contact(
                    f"{project_id} 联系人",
                    f"{project_id}@example.test",
                    aliases=[project_id],
                    project_ids=[project_id],
                    contact_id=f"contact-{project_id}",
                )
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        sent_attachments = {}

        def fake_send(_plan, attachments):
            sent_attachments["value"] = attachments
            return {"provider": "qq", "attachment_count": len(attachments)}

        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch(
            "core.mcp_server.get_workspace_keys",
            return_value=SimpleNamespace(document_key=b"0" * 32, metadata_key=b"1" * 32),
        ), patch("core.mcp_server.send_confirmed_email", side_effect=fake_send), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            for project_id, payload in (("project-a", b"project-a bytes"), ("project-b", b"project-b bytes")):
                uploaded = client.post(
                    "/drafts/email/attachments",
                    headers=headers,
                    data={"project_id": project_id},
                    files={"attachments": ("report.pdf", payload, "application/pdf")},
                )
                assert uploaded.status_code == 200
            created = client.post(
                "/drafts/email",
                headers=headers,
                json={
                    "project_id": "project-b",
                    "recipient_query": "project-b",
                    "subject": "项目 B 附件",
                    "body": "只允许发送项目 B 的同名附件。",
                    "attachment_refs": ["report.pdf"],
                },
            )
            assert created.json()["artifact"]["plan"]["attachment_bindings"][0]["status"] == "resolved_secure", created.json()
            artifact_id = created.json()["artifact"]["artifact_id"]
            confirmed = client.post(
                f"/drafts/{artifact_id}/status",
                params={"project_id": "project-b"},
                headers=headers,
                json={"status": "confirmed"},
            )
            sent = client.post(
                f"/drafts/{artifact_id}/send",
                params={"project_id": "project-b"},
                headers=headers,
                json={"sender": "sender@example.test", "idempotency_key": "c" * 16},
            )

    assert created.status_code == 200
    assert confirmed.status_code == 200
    assert sent.status_code == 200, sent.json()
    assert sent_attachments["value"] == [("report.pdf", b"project-b bytes")]


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


def test_contact_delete_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_contact("待删联系人", "delete@example.test", contact_id="delete-contact")
        finally:
            catalog.close()
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).delete(
                "/contacts/delete-contact",
                headers={"X-SoulDrive-Token": "draft-token"},
            )
        catalog = ProjectCatalog(workspace)
        try:
            remaining = [item for item in catalog.list_contacts() if item["contact_id"] == "delete-contact"]
        finally:
            catalog.close()

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert "审计记录写入失败" in response.json()["warning"]
    assert remaining == []


def test_draft_status_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            artifact_id = catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "审计故障确认", "start_at": "2026-09-01T10:00:00+08:00"},
            )
        finally:
            catalog.close()
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                f"/drafts/{artifact_id}/status",
                headers={"X-SoulDrive-Token": "draft-token"},
                json={"status": "confirmed"},
            )
        catalog = ProjectCatalog(workspace)
        try:
            persisted = catalog.get_artifact(artifact_id)
        finally:
            catalog.close()

    assert response.status_code == 200
    assert response.json()["artifact"]["confirmation_status"] == "confirmed"
    assert "审计记录写入失败" in response.json()["warning"]
    assert persisted["confirmation_status"] == "confirmed"


def test_smtp_configuration_stays_successful_when_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch(
            "core.mcp_server.configure_smtp",
            return_value={"configured": True, "provider": "qq", "sender": "sender@example.test"},
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                "/email/smtp-config",
                headers={"X-SoulDrive-Token": "draft-token"},
                json={"provider": "qq", "sender": "sender@example.test", "authorization_code": "private-code"},
            )

    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert "审计记录写入失败" in response.json()["warning"]


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
            rejected = client.post(f"/drafts/{draft_id}/send", params={"project_id": "demo"}, headers=headers, json={"sender": "sender@example.test", "idempotency_key": "a" * 16})
            sent = client.post(f"/drafts/{confirmed_id}/send", params={"project_id": "demo"}, headers=headers, json={"sender": "sender@example.test", "idempotency_key": "b" * 16})
            duplicate = client.post(f"/drafts/{confirmed_id}/send", params={"project_id": "demo"}, headers=headers, json={"sender": "sender@example.test", "idempotency_key": "b" * 16})

    assert rejected.status_code == 409
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"
    assert sent.json()["idempotent"] is False
    assert duplicate.status_code == 200
    assert duplicate.json()["idempotent"] is True


def test_email_send_stays_sent_when_success_audit_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            confirmed_id = catalog.create_artifact(
                "demo",
                "email_draft",
                {"recipient": {"email": "li@example.test"}, "subject": "审计故障发送", "body": "不要重复发送。"},
                confirmation_status="confirmed",
            )
        finally:
            catalog.close()
        audit = MagicMock()
        audit.append_event.side_effect = RuntimeError("audit unavailable")
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch(
            "core.mcp_server.get_workspace_keys", return_value=object()
        ), patch("core.mcp_server.SecureDocumentStore"), patch(
            "core.mcp_server.send_confirmed_email",
            return_value={"provider": "mock", "attachment_count": 0},
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app).post(
                f"/drafts/{confirmed_id}/send",
                params={"project_id": "demo"},
                headers={"X-SoulDrive-Token": "draft-token"},
                json={"sender": "sender@example.test", "idempotency_key": "audit-send-001-01"},
            )
            assert response.status_code == 200, response.text
        catalog = ProjectCatalog(workspace)
        try:
            delivery_status = catalog.conn.execute(
                "SELECT status FROM email_deliveries WHERE artifact_id = ?", (confirmed_id,)
            ).fetchone()["status"]
        finally:
            catalog.close()

    assert response.status_code == 200
    assert response.json()["status"] == "sent"
    assert response.json()["idempotent"] is False
    assert "审计记录写入失败" in response.json()["warning"]
    assert delivery_status == "sent"


def test_email_send_rejects_same_key_with_different_sender_parameters():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            confirmed_id = catalog.create_artifact(
                "demo",
                "email_draft",
                {"recipient": {"email": "li@example.test"}, "subject": "幂等参数", "body": "同键不同参数应拒绝。"},
                confirmation_status="confirmed",
            )
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch(
            "core.mcp_server.get_workspace_keys", return_value=object()
        ), patch("core.mcp_server.SecureDocumentStore"), patch(
            "core.mcp_server.send_confirmed_email",
            return_value={"provider": "mock", "sender": "sender-a@example.test", "attachment_count": 0},
        ) as send_mock, patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            sent = client.post(
                f"/drafts/{confirmed_id}/send",
                params={"project_id": "demo"},
                headers=headers,
                json={"sender": "sender-a@example.test", "idempotency_key": "m" * 16},
            )
            changed = client.post(
                f"/drafts/{confirmed_id}/send",
                params={"project_id": "demo"},
                headers=headers,
                json={"sender": "sender-b@example.test", "idempotency_key": "m" * 16},
            )

    assert sent.status_code == 200
    assert changed.status_code == 409
    assert "idempotency" in changed.json()["error"]
    assert send_mock.call_count == 1


def test_email_send_marks_external_outcome_uncertain_after_provider_exception():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            confirmed_id = catalog.create_artifact(
                "demo",
                "email_draft",
                {"recipient": {"email": "li@example.test"}, "subject": "结果未知", "body": "请确认。"},
                confirmation_status="confirmed",
            )
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch(
            "core.mcp_server.get_workspace_keys", return_value=object()
        ), patch("core.mcp_server.SecureDocumentStore"), patch(
            "core.mcp_server.send_confirmed_email", side_effect=RuntimeError("provider connection lost")
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            response = client.post(
                f"/drafts/{confirmed_id}/send",
                params={"project_id": "demo"},
                headers=headers,
                json={"sender": "sender@example.test", "idempotency_key": "u" * 16},
            )
            replay = client.post(
                f"/drafts/{confirmed_id}/send",
                params={"project_id": "demo"},
                headers=headers,
                json={"sender": "sender@example.test", "idempotency_key": "u" * 16},
            )
            persisted = ProjectCatalog(workspace)
            try:
                persisted_status = persisted.reserve_email_delivery(confirmed_id, "u" * 16)
            finally:
                persisted.close()

    assert response.status_code == 502
    assert response.json()["delivery_status"] == "uncertain"
    assert replay.status_code == 409
    assert replay.json()["delivery_status"] == "uncertain"

    assert persisted_status == "uncertain"


def test_smtp_delivery_error_after_send_boundary_is_uncertain_and_not_retryable():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            confirmed_id = catalog.create_artifact(
                "demo",
                "email_draft",
                {"recipient": {"email": "li@example.test"}, "subject": "SMTP异常", "body": "请勿重复发送。"},
                confirmation_status="confirmed",
            )
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        events = []
        audit = SimpleNamespace(
            append_event=lambda event_type, payload=None, **_kwargs: events.append((event_type, payload))
        )
        secret_error = "provider echoed secret=SMTP-TOKEN private-recipient@example.test"
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch(
            "core.mcp_server.get_workspace_keys", return_value=object()
        ), patch("core.mcp_server.SecureDocumentStore"), patch(
            "core.mcp_server.send_confirmed_email",
            side_effect=mcp_server.EmailDeliveryError(secret_error),
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            response = client.post(
                f"/drafts/{confirmed_id}/send",
                params={"project_id": "demo"},
                headers=headers,
                json={"sender": "sender@example.test", "idempotency_key": "e" * 16},
            )
            replay = client.post(
                f"/drafts/{confirmed_id}/send",
                params={"project_id": "demo"},
                headers=headers,
                json={"sender": "sender@example.test", "idempotency_key": "e" * 16},
            )
            persisted = ProjectCatalog(workspace)
            try:
                delivery_detail = persisted.conn.execute(
                    "SELECT detail_json FROM email_deliveries WHERE artifact_id = ?",
                    (confirmed_id,),
                ).fetchone()["detail_json"]
            finally:
                persisted.close()

    assert response.status_code == 502
    assert response.json()["delivery_status"] == "uncertain"
    assert replay.status_code == 409
    assert replay.json()["delivery_status"] == "uncertain"
    assert secret_error not in json.dumps(events, ensure_ascii=False)
    assert secret_error not in delivery_detail


def test_smtp_delivery_error_stays_uncertain_when_receipt_persistence_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            confirmed_id = catalog.create_artifact(
                "demo",
                "email_draft",
                {"recipient": {"email": "li@example.test"}, "subject": "回执故障", "body": "请确认。"},
                confirmation_status="confirmed",
            )
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch(
            "core.mcp_server.get_workspace_keys", return_value=object()
        ), patch("core.mcp_server.SecureDocumentStore"), patch(
            "core.mcp_server.send_confirmed_email",
            side_effect=mcp_server.EmailDeliveryError("SMTP response lost"),
        ), patch.object(
            ProjectCatalog,
            "finish_email_delivery",
            side_effect=OSError("catalog receipt unavailable"),
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            response = TestClient(mcp_server.app, raise_server_exceptions=False).post(
                f"/drafts/{confirmed_id}/send",
                params={"project_id": "demo"},
                headers={"X-SoulDrive-Token": "draft-token"},
                json={"sender": "sender@example.test", "idempotency_key": "receipt-failure-001"},
            )
        persisted = ProjectCatalog(workspace)
        try:
            retry_status = persisted.reserve_email_delivery(confirmed_id, "receipt-failure-001")
        finally:
            persisted.close()

    assert response.status_code == 502
    assert response.json()["delivery_status"] == "uncertain"
    assert retry_status == "uncertain"


def test_email_preflight_exception_remains_retryable():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("演示项目", project_id="demo")
            confirmed_id = catalog.create_artifact(
                "demo",
                "email_draft",
                {"recipient": {"email": "li@example.test"}, "subject": "准备失败", "body": "请重试。"},
                confirmation_status="confirmed",
            )
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch(
            "core.mcp_server.get_workspace_keys", return_value=object()
        ), patch(
            "core.mcp_server.SecureDocumentStore",
            side_effect=RuntimeError("local store unavailable secret=SMTP-TOKEN path=C:\\private\\mail.eml"),
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            response = client.post(
                f"/drafts/{confirmed_id}/send",
                params={"project_id": "demo"},
                headers=headers,
                json={"sender": "sender@example.test", "idempotency_key": "p" * 16},
            )
            persisted = ProjectCatalog(workspace)
            try:
                persisted_detail = persisted.conn.execute(
                    "SELECT detail_json FROM email_deliveries WHERE artifact_id = ?",
                    (confirmed_id,),
                ).fetchone()["detail_json"]
                persisted_status = persisted.reserve_email_delivery(confirmed_id, "q" * 16)
            finally:
                persisted.close()

    assert response.status_code == 502
    assert response.json()["delivery_status"] == "failed"
    assert "SMTP-TOKEN" not in response.text
    assert "C:\\private\\mail.eml" not in response.text
    assert "SMTP-TOKEN" not in persisted_detail
    assert "C:\\private\\mail.eml" not in persisted_detail
    assert "邮件发送失败" in persisted_detail
    assert "error_observation" in persisted_detail
    assert persisted_status == "reserved"


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
            listed = client.get("/schedules", headers=headers)

    assert created.status_code == 200
    assert created.json()["artifact"]["project_id"] is None
    assert created.json()["artifact"]["confirmation_record"]["required"] is True
    assert "未写入外部日历" in created.json()["artifact"]["change_summary"]
    assert "T10:00:00" in created.json()["artifact"]["plan"]["start_at"]
    assert created.json()["artifact"]["plan"]["reminder_sound_enabled"] is True
    assert listed.status_code == 200
    assert [item["artifact_id"] for item in listed.json()["artifacts"]] == [created.json()["artifact"]["artifact_id"]]


def test_confirmed_schedule_ics_export_replay_is_byte_stable():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        events = []
        audit = SimpleNamespace(append_event=lambda event_type, payload=None: events.append((event_type, payload)))
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            created = client.post(
                "/drafts/schedule",
                headers=headers,
                json={"title": "稳定 ICS", "start_at": "2026-09-01T10:00", "duration_minutes": 30},
            )
            artifact_id = created.json()["artifact"]["artifact_id"]
            confirmed = client.post(
                f"/drafts/{artifact_id}/status",
                headers=headers,
                json={"status": "confirmed"},
            )
            first = client.post(f"/drafts/{artifact_id}/calendar-ics", headers=headers)
            first_bytes = Path(first.json()["output_path"]).read_bytes()
            second = client.post(f"/drafts/{artifact_id}/calendar-ics", headers=headers)
            second_bytes = Path(second.json()["output_path"]).read_bytes()

    assert created.status_code == 200
    assert confirmed.status_code == 200
    assert first.status_code == 200
    assert first.json()["replayed"] is False
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert first.json()["output_path"] == second.json()["output_path"]
    assert first_bytes == second_bytes
    assert [event_type for event_type, _ in events if event_type == "schedule.ics_exported"] == ["schedule.ics_exported"]


def test_confirmed_schedule_ics_export_concurrent_requests_publish_one_receipt():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            artifact_id = catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "并发 ICS", "start_at": "2026-09-01T10:00:00+08:00", "duration_minutes": 30},
                confirmation_status="confirmed",
            )
        finally:
            catalog.close()

        state_path = str(Path(temp_dir) / "runtime_state.json")
        events = []
        audit = SimpleNamespace(append_event=lambda event_type, payload=None: events.append((event_type, payload)))
        first_export_started = threading.Event()
        release_first_export = threading.Event()
        export_calls = 0
        export_calls_lock = threading.Lock()

        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            original_export = mcp_server.export_ics

            def gated_export(*args, **kwargs):
                nonlocal export_calls
                with export_calls_lock:
                    export_calls += 1
                    call_number = export_calls
                if call_number == 1:
                    first_export_started.set()
                    assert release_first_export.wait(5)
                return original_export(*args, **kwargs)

            responses = []

            def export_request():
                responses.append(client.post(f"/drafts/{artifact_id}/calendar-ics", headers=headers))

            with patch("core.mcp_server.export_ics", side_effect=gated_export):
                first = threading.Thread(target=export_request)
                first.start()
                assert first_export_started.wait(5)
                second = threading.Thread(target=export_request)
                second.start()
                second.join(0.5)
                release_first_export.set()
                first.join(10)
                second.join(10)

    assert len(responses) == 2
    assert all(response.status_code == 200 for response in responses)
    assert export_calls == 1
    assert sorted(response.json()["replayed"] for response in responses) == [False, True]
    assert [event_type for event_type, _ in events if event_type == "schedule.ics_exported"] == ["schedule.ics_exported"]


def test_schedule_ics_export_refuses_to_replay_after_output_was_changed():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            created = client.post(
                "/drafts/schedule",
                headers=headers,
                json={"title": "受保护 ICS", "start_at": "2026-09-01T10:00", "duration_minutes": 30},
            )
            artifact_id = created.json()["artifact"]["artifact_id"]
            client.post(f"/drafts/{artifact_id}/status", headers=headers, json={"status": "confirmed"})
            first = client.post(f"/drafts/{artifact_id}/calendar-ics", headers=headers)
            output_path = Path(first.json()["output_path"])
            output_path.write_text("BEGIN:VCALENDAR\r\nX-EDITED-BY-USER:1\r\nEND:VCALENDAR\r\n", encoding="utf-8")
            replay = client.post(f"/drafts/{artifact_id}/calendar-ics", headers=headers)
            edited_bytes = output_path.read_bytes()

    assert first.status_code == 200
    assert replay.status_code == 409
    assert replay.json()["error_code"] == "calendar_export_output_conflict"
    assert "重新确认" in replay.json()["error"]
    assert b"X-EDITED-BY-USER:1" in edited_bytes


def test_schedule_ics_export_keeps_success_when_audit_sink_fails():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            artifact_id = catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "审计故障 ICS", "start_at": "2026-09-01T10:00:00+08:00", "duration_minutes": 30},
                confirmation_status="confirmed",
            )
        finally:
            catalog.close()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        audit = SimpleNamespace(append_event=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")))
        with patch.object(runtime_state, "STATE_PATH", state_path), patch(
            "core.mcp_server.current_workspace", return_value=workspace
        ), patch("core.mcp_server.current_audit_logger", return_value=audit), patch.dict(
            "os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False
        ):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            response = client.post(f"/drafts/{artifact_id}/calendar-ics", headers=headers)
            replay = client.post(f"/drafts/{artifact_id}/calendar-ics", headers=headers)

    assert response.status_code == 200
    assert response.json()["replayed"] is False
    assert "审计记录写入失败" in response.json()["warning"]
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_schedule_history_keeps_recent_elapsed_records_but_excludes_old_records():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            recent_id = catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "近期日程", "start_at": "2026-08-24T10:00:00+00:00"},
            )
            elapsed_id = catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "已结束日程", "start_at": "2026-08-20T10:00:00+00:00"},
                confirmation_status="confirmed",
            )
            cancelled_id = catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "已取消日程", "start_at": "2026-08-21T10:00:00+00:00"},
            )
            catalog.set_artifact_status(cancelled_id, "cancelled")
            old_id = catalog.create_artifact(
                None,
                "schedule_draft",
                {"title": "过期历史", "start_at": "2026-08-01T10:00:00+00:00"},
            )
            old_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
            catalog.conn.execute(
                "UPDATE artifacts SET created_at = ? WHERE artifact_id = ?",
                (old_timestamp, old_id),
            )
            catalog.conn.commit()
            history = catalog.list_schedule_history(days=7)
        finally:
            catalog.close()

    history_ids = [item["artifact_id"] for item in history]
    assert recent_id in history_ids
    assert elapsed_id in history_ids
    assert cancelled_id in history_ids
    assert old_id not in history_ids
    assert history[0]["created_at"] >= history[-1]["created_at"]


def test_schedule_history_endpoint_returns_recent_records_and_existing_detail_route():
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        state_path = str(Path(temp_dir) / "runtime_state.json")
        with patch.object(runtime_state, "STATE_PATH", state_path), patch("core.mcp_server.current_workspace", return_value=workspace), patch.dict("os.environ", {"SOULDRIVE_API_TOKEN": "draft-token"}, clear=False):
            runtime_state.unlock_runtime("PRO", "SN-1", temp_dir, workspace.root_path)
            runtime_state.mark_software_unlocked()
            client = TestClient(mcp_server.app)
            headers = {"X-SoulDrive-Token": "draft-token"}
            created = client.post(
                "/drafts/schedule",
                headers=headers,
                json={"title": "历史入口测试", "start_at": "2030-09-01T10:00", "duration_minutes": 30},
            )
            artifact_id = created.json()["artifact"]["artifact_id"]
            history = client.get("/schedules/history", headers=headers)
            detail = client.get(f"/drafts/{artifact_id}", headers=headers)

    assert created.status_code == 200
    assert history.status_code == 200
    assert history.json()["days"] == 7
    assert artifact_id in {item["artifact_id"] for item in history.json()["artifacts"]}
    assert detail.status_code == 200
    assert detail.json()["artifact"]["plan"]["title"] == "历史入口测试"


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
