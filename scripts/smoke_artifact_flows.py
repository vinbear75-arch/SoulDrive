"""Run side-effect-free PPT, email MIME, and project Skill smoke checks."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.email_connector import send_confirmed_email
from core.presentation_service import PresentationService, find_wps_presentation_executable, verify_pptx
from core.project_skill_installer import activate_skill_from_inbox, approve_skill_from_inbox, install_skill_to_inbox, list_skill_inbox
from core.project_skills import discover_project_skills
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


def _response(payload: bytes):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    return Response(payload)


def _skill_opener(request, timeout=0):
    if timeout != 20:
        raise AssertionError("unexpected Skill download timeout")
    return _response(
        b"---\nname: Smoke Skill\ndescription: Verify a local workflow\n---\n1. Check inputs\n2. Verify output\n"
    )


def run(output_path: Path) -> dict:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    presentation_copy = output_path.with_suffix(".pptx")
    with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
        os.environ, {"SOULDRIVE_PRESENTATION_RENDERER": "openxml"}
    ):
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        initialize_keystore(workspace, "smoke-passphrase")
        keys = unlock_keystore(workspace, "smoke-passphrase")
        connection = sqlite3.connect(workspace.parent_doc_path)
        connection.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
        connection.execute(
            "INSERT INTO parent_documents VALUES (?, ?, ?)",
            (
                "smoke_parent",
                "SoulDrive 在本地完成文档检索、证据校验和组会汇报生成。该演示只使用已入库资料。",
                json.dumps({"source_filename": "smoke-paper.md", "section": "结论", "page_number": 1, "parent_index": 0}, ensure_ascii=False),
            ),
        )
        connection.commit()
        connection.close()

        presentation = PresentationService(workspace, keys)
        task = presentation.create_plan(
            {
                "source_filenames": ["smoke-paper.md"],
                "title": "SoulDrive 本地科研汇报",
                "audience": "technical",
                "max_slides": 6,
                "open_in_wps": False,
            }
        )
        planned = presentation.run_task(task["task_id"])
        plan_result = planned["result"]
        no_pptx_before_confirmation = not any(Path(workspace.presentations_path).glob("*.pptx"))
        generation = presentation.confirm_plan(task["task_id"], plan_result["plan_digest"])
        task_result = presentation.run_task(generation["task_id"])
        result = task_result["result"]
        generated = Path(result["output_path"])
        generated_digest = hashlib.sha256(generated.read_bytes()).hexdigest()
        reopened = PresentationService(workspace, keys)
        replayed_generation = reopened.confirm_plan(task["task_id"], plan_result["plan_digest"])
        confirmation_replayed_same_task = replayed_generation["task_id"] == generation["task_id"]
        replayed = reopened.run_task(generation["task_id"])
        reopened_result_unchanged = (
            replayed["result"] == result
            and hashlib.sha256(generated.read_bytes()).hexdigest() == generated_digest
        )
        reopened_task_status = reopened.get_task(generation["task_id"])["status"]
        shutil.copy2(generated, presentation_copy)
        ppt_report = verify_pptx(presentation_copy, result["slide_count"])

        smtp_client = MagicMock()
        with ExitStack() as stack:
            stack.enter_context(
                patch(
                    "core.email_connector._read_config",
                    return_value={
                        "provider": "qq",
                        "host": "smtp.qq.com",
                        "port": 465,
                        "sender": "sender@example.test",
                        "authorization_code": "not-written-to-result",
                    },
                )
            )
            smtp_factory = stack.enter_context(patch("core.email_connector.smtplib.SMTP_SSL"))
            smtp_factory.return_value.__enter__.return_value = smtp_client
            email_result = send_confirmed_email(
                {
                    "sender": "sender@example.test",
                    "recipient": {"email": "recipient@example.test"},
                    "subject": "SoulDrive smoke",
                    "body": "Please review the attached report.",
                    "idempotency_key": "smoke-email-0123456",
                },
                [("smoke.txt", b"evidence")],
            )
            message = smtp_client.send_message.call_args.args[0]

        skills_path = workspace.skills_path
        installed = install_skill_to_inbox(
            skills_path,
            "https://raw.githubusercontent.com/acme/research/main/SKILL.md",
            "smoke",
            opener=_skill_opener,
        )
        inbox_before = list_skill_inbox(skills_path)
        approved = approve_skill_from_inbox(skills_path, installed["skill_name"])
        skills_before_activation = discover_project_skills(skills_path)
        activated = activate_skill_from_inbox(skills_path, installed["skill_name"])
        active_skills = discover_project_skills(skills_path)

    report = {
        "schema_version": "souldrive_artifact_smoke_v1",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "email": {
            "status": "pass",
            "provider": email_result["provider"],
            "smtp_target": [smtp_factory.call_args.args[0], smtp_factory.call_args.args[1]],
            "recipient": message["To"],
            "attachment_count": len(list(message.iter_attachments())),
            "authorization_code_exposed": "not-written-to-result" in json.dumps(email_result),
        },
        "ppt": {
            "status": "pass" if (
                planned["status"] == "completed"
                and plan_result["phase"] == "outline_review"
                and no_pptx_before_confirmation
                and task_result["status"] == "completed"
                and ppt_report["valid"]
                and confirmation_replayed_same_task
                and reopened_result_unchanged
                and reopened_task_status == "completed"
            ) else "fail",
            "outline_review_phase": plan_result["phase"],
            "plan_task_id": task["task_id"],
            "plan_digest": plan_result["plan_digest"],
            "generation_task_id": generation["task_id"],
            "no_pptx_before_confirmation": no_pptx_before_confirmation,
            "confirmation_replayed_same_task": confirmation_replayed_same_task,
            "reopened_result_unchanged": reopened_result_unchanged,
            "reopened_task_status": reopened_task_status,
            "renderer": result["renderer"],
            "slide_count": result["slide_count"],
            "outline_source": result["outline_source"],
            "evidence_metrics": result["evidence_metrics"],
            "elapsed_ms": result["elapsed_ms"],
            "valid_openxml": ppt_report["valid"],
            "bytes": presentation_copy.stat().st_size,
            "sha256": hashlib.sha256(presentation_copy.read_bytes()).hexdigest(),
            "zip_entries": len(zipfile.ZipFile(presentation_copy).namelist()),
            "output_path": str(presentation_copy),
            "wps_executable": find_wps_presentation_executable(),
            "wps_open_requested": False,
        },
        "skill": {
            "status": "pass" if (
                approved["status"] == "pending_review"
                and not skills_before_activation
                and activated["status"] == "active"
                and active_skills
            ) else "fail",
            "download_status": installed["status"],
            "inbox_count_before_approval": len(inbox_before),
            "approval_status": approved["status"],
            "active_before_activation": bool(skills_before_activation),
            "activation_status": activated["status"],
            "active_skill_title": active_skills[0]["title"] if active_skills else None,
            "scripts_auto_executed": False,
        },
    }
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="eval/results/artifact_smoke_current.json")
    args = parser.parse_args()
    report = run(Path(args.output).resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(section["status"] == "pass" for section in (report["email"], report["ppt"], report["skill"])) else 1


if __name__ == "__main__":
    raise SystemExit(main())
