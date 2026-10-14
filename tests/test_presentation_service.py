import json
import hashlib
import sqlite3
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from core import presentation_service as presentation_service_module
from core.presentation_service import (
    PRESENTATION_PLAN_STEPS,
    PresentationService,
    _write_json_marker,
    build_extractable_presentation_plan,
    collect_indexed_evidence,
    presentation_filename,
    presentation_evidence_metrics,
    _presentation_repetition_inputs,
    validate_presentation_request,
    verify_pptx,
)
from core.openxml_presentation import render_presentation
from core.project_catalog import ProjectCatalog
from core.task_journal import TaskJournal, TaskPublicError
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class PresentationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workspace = SoulDriveWorkspace.from_drive(self.temp_dir.name).ensure()

    def test_request_requires_selected_indexed_document_names(self):
        request = validate_presentation_request(
            {
                "source_filenames": ["A方案.pdf", "A方案.pdf", "B政策.pdf"],
                "audience": "government",
                "max_slides": 20,
                "open_in_wps": False,
            }
        )

        self.assertEqual(request["source_filenames"], ["A方案.pdf", "B政策.pdf"])
        self.assertEqual(request["max_slides"], 16)

    def test_presentation_evidence_metrics_do_not_collapse_same_name_object_ids(self):
        metrics = presentation_evidence_metrics(
            {
                "source_filenames": ["同名.pdf", "同名.pdf"],
                "source_document_ids": ["object-a", "object-b"],
                "slides": [
                    {"kind": "cover", "sources": []},
                    {"kind": "evidence", "sources": ["同名.pdf"], "points": ["事实"]},
                ],
            },
            2,
            [
                {"source_filename": "同名.pdf", "document_id": "object-a"},
                {"source_filename": "同名.pdf", "document_id": "object-b"},
            ],
        )

        self.assertEqual(metrics["selected_source_count"], 2)
        self.assertEqual(metrics["selected_source_label_count"], 1)
        self.assertEqual(metrics["selected_label_coverage"], 1.0)
        self.assertEqual(metrics["selected_identity_count"], 2)
        self.assertEqual(metrics["cited_identity_count"], 0)
        self.assertEqual(metrics["selected_source_coverage"], 0.0)
        self.assertEqual(metrics["source_identity_coverage_status"], "ambiguous_display_labels")

    def test_presentation_repetition_projection_excludes_execution_ids(self):
        base = {
            "request_id": "request-1",
            "task_id": "task-1",
            "artifact_id": "artifact-1",
            "source_filenames": ["A.pdf"],
            "source_version_ids": ["version-a"],
            "title": "方案汇报",
            "audience": "research",
            "max_slides": 8,
            "open_in_wps": False,
            "style_skill_id": "",
        }
        repeated = dict(base)
        repeated.update({
            "request_id": "request-2",
            "task_id": "task-2",
            "artifact_id": "artifact-2",
        })

        self.assertEqual(
            _presentation_repetition_inputs(base),
            _presentation_repetition_inputs(repeated),
        )
        changed = dict(repeated, title="另一份汇报")
        self.assertNotEqual(
            _presentation_repetition_inputs(base),
            _presentation_repetition_inputs(changed),
        )

    def test_presentation_audit_failure_does_not_mask_task_state(self):
        class FailingAuditLogger:
            def append_event(self, *_args, **_kwargs):
                raise OSError("audit-secret-presentation")

        service = PresentationService(
            self.workspace,
            {},
            audit_logger=FailingAuditLogger(),
        )

        service._audit(
            "presentation.completed",
            {"task_id": "task-1", "status": "completed"},
            {"output_path": "secret-output.pptx"},
        )

    def test_presentation_plan_warns_after_failed_business_input_with_new_request_id(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        service = PresentationService(self.workspace, keys)
        first = service.create_plan({
            "request_id": "request-1",
            "source_filenames": ["A方案.pdf"],
            "title": "同一业务汇报",
            "open_in_wps": False,
        })

        journal = TaskJournal(self.workspace, keys)
        try:
            failed = journal.run_steps(
                first["task_id"],
                "worker-1",
                {
                    "collect_evidence": lambda _inputs, _outputs: (_ for _ in ()).throw(
                        RuntimeError("simulated plan failure")
                    ),
                    "build_outline": lambda _inputs, _outputs: {"ok": True},
                },
                lambda outputs: outputs,
            )
        finally:
            journal.close()
        self.assertEqual(failed["status"], "failed")

        repeated = service.create_plan({
            "request_id": "request-2",
            "source_filenames": ["A方案.pdf"],
            "title": "同一业务汇报",
            "open_in_wps": False,
        })
        self.assertEqual(repeated["repetition_guard"]["decision"], "warn")
        self.assertEqual(repeated["repetition_guard"]["basis"], "business_projection")
        self.assertEqual(repeated["repetition_guard"]["matches"][0]["task_id"], first["task_id"])

    def test_completed_presentation_task_replay_does_not_duplicate_completion_audit(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        journal = TaskJournal(self.workspace, keys)
        try:
            task = journal.create_task(
                "presentation.plan",
                {"request_id": "completed-replay", "source_filenames": ["report.pdf"]},
                PRESENTATION_PLAN_STEPS,
                task_id="completed-presentation-task",
            )
            completed = journal.run_steps(
                task["task_id"],
                "test-owner",
                {step: (lambda _inputs, _outputs: {"ok": True}) for step in PRESENTATION_PLAN_STEPS},
                lambda outputs: {"status": "completed", "outputs": outputs},
            )
        finally:
            journal.close()

        self.assertEqual(completed["status"], "completed")
        events = []
        service = PresentationService(
            self.workspace,
            keys,
            audit_logger=SimpleNamespace(
                append_event=lambda event_type, payload=None: events.append((event_type, payload)),
            ),
        )
        replayed = service.run_task(task["task_id"])

        self.assertEqual(replayed["status"], "completed")
        self.assertEqual(events, [])

    def test_create_plan_replay_marks_existing_task_without_persisting_transport_metadata(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        service = PresentationService(self.workspace, keys)
        request = {
            "request_id": "plan-replay-metadata",
            "source_filenames": ["report.pdf"],
            "title": "回放状态",
            "open_in_wps": False,
        }

        first = service.create_plan(request)
        replay = service.create_plan(request)

        self.assertNotIn("replayed", first)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["task_id"], first["task_id"])
        journal = TaskJournal(self.workspace, keys)
        try:
            persisted = journal.get_task(first["task_id"])
        finally:
            journal.close()
        self.assertNotIn("replayed", persisted)

    def test_collect_indexed_evidence_normalizes_case_insensitive_source_name(self):
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "case-parent",
                    "Windows 文件名匹配应保持稳定。",
                    json.dumps({"source_filename": "report.pdf", "page_number": 1}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        records = collect_indexed_evidence(self.workspace, None, ["REPORT.PDF"])

        self.assertEqual([record["source_filename"] for record in records], ["REPORT.PDF"])

    def test_collect_indexed_evidence_keeps_same_name_documents_separate_by_id(self):
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.executemany(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                [
                    (
                        "same-name-a",
                        "甲对象的证据。",
                        json.dumps({"source_filename": "同名报告.pdf", "document_id": "secure_object-a"}),
                    ),
                    (
                        "same-name-b",
                        "乙对象的证据。",
                        json.dumps({"source_filename": "同名报告.pdf", "document_id": "secure_object-b"}),
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        records = collect_indexed_evidence(
            self.workspace,
            None,
            ["同名报告.pdf", "同名报告.pdf"],
            source_document_ids=["object-a", "object-b"],
        )

        self.assertEqual(
            [(record["document_id"], record["content"]) for record in records],
            [("secure_object-a", "甲对象的证据。"), ("secure_object-b", "乙对象的证据。")],
        )

    def test_presentation_request_requires_display_labels_for_object_ids(self):
        request = validate_presentation_request({
            "source_filenames": ["同名报告.pdf", "同名报告.pdf"],
            "source_document_ids": ["object-a", "object-b"],
        })

        self.assertEqual(request["source_filenames"], ["同名报告.pdf", "同名报告.pdf"])
        self.assertEqual(request["source_document_ids"], ["object-a", "object-b"])
        with self.assertRaisesRegex(ValueError, "display labels"):
            validate_presentation_request({"source_document_ids": ["object-a"]})
        with self.assertRaisesRegex(ValueError, "same length"):
            validate_presentation_request({
                "source_filenames": ["同名报告.pdf", "另一份.pdf"],
                "source_document_ids": ["object-a"],
            })

    def test_presentation_plan_rejects_ambiguous_name_without_object_id(self):
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.executemany(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                [
                    ("ambiguous-a", "甲对象内容。", json.dumps({
                        "source_filename": "重复.pdf", "document_id": "object-a",
                    })),
                    ("ambiguous-b", "乙对象内容。", json.dumps({
                        "source_filename": "重复.pdf", "document_id": "object-b",
                    })),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        service = PresentationService(self.workspace, keys)
        task = service.create_plan({"source_filenames": ["重复.pdf"], "open_in_wps": False})
        result = service.run_task(task["task_id"])

        self.assertEqual(result["status"], "failed")
        self.assertIn("exact document IDs", result["error"])

    def test_presentation_plan_persists_exact_object_ids_through_outline_snapshot(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.executemany(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                [
                    ("selected-a", "甲对象的研究结论已经完成验证。", json.dumps({
                        "source_filename": "同名论文.pdf", "document_id": "secure_object-a",
                    })),
                    ("selected-b", "乙对象的研究结论已经完成验证。", json.dumps({
                        "source_filename": "同名论文.pdf", "document_id": "secure_object-b",
                    })),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        service = PresentationService(self.workspace, keys)
        task = service.create_plan({
            "request_id": "object-bound-presentation",
            "source_filenames": ["同名论文.pdf", "同名论文.pdf"],
            "source_document_ids": ["object-a", "object-b"],
            "open_in_wps": False,
        })
        planned = service.run_task(task["task_id"])

        self.assertEqual(planned["status"], "completed")
        self.assertEqual(task["inputs"]["source_document_ids"], ["object-a", "object-b"])
        self.assertEqual(
            [ref["document_id"] for ref in planned["result"]["source_refs"]],
            ["object-a", "object-b"],
        )
        self.assertEqual(set(planned["result"]["source_fingerprints"]), {"object-a", "object-b"})

    def test_collects_only_selected_parent_documents_and_builds_source_bound_plan(self):
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.executemany(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                [
                    (
                        "p1",
                        "本项目建设统一数据平台，采用分级授权和本地加密机制。项目分三个阶段实施。",
                        json.dumps({"source_filename": "A方案.pdf", "section": "总体方案", "page_number": 3}),
                    ),
                    (
                        "p2",
                        "政策要求建立数据分类分级制度，并明确安全责任和监督机制。",
                        json.dumps({"source_filename": "B政策.pdf", "section": "安全要求", "page_number": 8}),
                    ),
                    (
                        "p3",
                        "不应进入结果的其他材料。",
                        json.dumps({"source_filename": "C噪声.pdf", "page_number": 1}),
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()
        records = collect_indexed_evidence(self.workspace, None, ["A方案.pdf", "B政策.pdf"])
        plan = build_extractable_presentation_plan(
            records,
            {
                "source_filenames": ["A方案.pdf", "B政策.pdf"],
                "title": "数据平台项目汇报",
                "audience": "government",
                "max_slides": 8,
                "open_in_wps": False,
            },
        )

        self.assertEqual({item["source_filename"] for item in records}, {"A方案.pdf", "B政策.pdf"})
        self.assertLessEqual(len(plan["slides"]), 8)
        self.assertEqual(plan["slides"][0]["kind"], "cover")
        self.assertEqual(plan["slides"][-1]["kind"], "sources")
        self.assertNotIn("C噪声.pdf", json.dumps(plan, ensure_ascii=False))

    def test_parent_index_fills_sources_missing_from_vector_index(self):
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                ("p1", "父文档索引仍可用于生成汇报。", json.dumps({"source_filename": "仅父索引.md", "page_number": 1})),
            )
            conn.commit()
        finally:
            conn.close()
        secure_path = Path(self.workspace.secure_vector_store_path)
        secure_path.parent.mkdir(parents=True, exist_ok=True)
        secure_path.touch()

        class EmptyVectorStore:
            def __init__(self, *_args, **_kwargs):
                self.collection = self

            def get(self, **_kwargs):
                return {"metadatas": []}

            def close(self):
                pass

        with patch("core.presentation_service.SecureVectorStore", EmptyVectorStore):
            records = collect_indexed_evidence(self.workspace, object(), ["仅父索引.md"])

        self.assertEqual([record["source_filename"] for record in records], ["仅父索引.md"])

    def test_project_presentation_rejects_foreign_project_evidence(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.create_project("项目一", project_id="project-one")
            catalog.create_project("项目二", project_id="project-two")
        finally:
            catalog.close()
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "foreign-parent",
                    "项目二的机密材料，不得进入项目一的 PPT。",
                    json.dumps({"source_filename": "p2-secret.pdf", "project_id": "project-two"}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(
            collect_indexed_evidence(self.workspace, keys, ["p2-secret.pdf"], project_id="project-one"),
            [],
        )

        class ForeignVectorStore:
            def __init__(self, *_args, **_kwargs):
                self.collection = self

            def get(self, **_kwargs):
                return {
                    "metadatas": [{
                        "source_filename": "p2-secret.pdf",
                        "parent_id": "foreign-parent",
                        "project_id": "project-two",
                    }]
                }

            def get_parent(self, _parent_id):
                return {
                    "content": "项目二的机密材料，不得进入项目一的 PPT。",
                    "metadata": {
                        "source_filename": "p2-secret.pdf",
                        "project_id": "project-two",
                    },
                }

            def close(self):
                pass

        secure_path = Path(self.workspace.secure_vector_store_path)
        secure_path.parent.mkdir(parents=True, exist_ok=True)
        secure_path.touch()
        with patch("core.presentation_service.SecureVectorStore", ForeignVectorStore):
            self.assertEqual(
                collect_indexed_evidence(self.workspace, keys, ["p2-secret.pdf"], project_id="project-one"),
                [],
            )

        service = PresentationService(self.workspace, keys)
        plan_task = service.create_plan({
            "project_id": "project-one",
            "source_filenames": ["p2-secret.pdf"],
            "open_in_wps": False,
        })
        result = service.run_task(plan_task["task_id"])

        self.assertEqual(result["status"], "failed")
        self.assertIn("not indexed", result["error"])
        self.assertEqual(list(Path(self.workspace.presentations_path).glob("*.pptx")), [])

    def test_project_presentation_rejects_conflicting_parent_child_scope_metadata(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")

        class ConflictingVectorStore:
            def __init__(self, *_args, **_kwargs):
                self.collection = self

            def get(self, **_kwargs):
                return {
                    "metadatas": [{
                        "source_filename": "p2-secret.pdf",
                        "parent_id": "conflicting-parent",
                        "project_id": "project-two",
                        "asset_version_id": "version-two",
                    }]
                }

            def get_parent(self, _parent_id):
                return {
                    "content": "项目二的材料，不能因父记录误标而进入项目一。",
                    "metadata": {
                        "source_filename": "p2-secret.pdf",
                        "project_id": "project-one",
                        "asset_version_id": "version-one",
                    },
                }

            def close(self):
                pass

        secure_path = Path(self.workspace.secure_vector_store_path)
        secure_path.parent.mkdir(parents=True, exist_ok=True)
        secure_path.touch()
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.commit()
        finally:
            conn.close()
        with patch("core.presentation_service.SecureVectorStore", ConflictingVectorStore):
            records = collect_indexed_evidence(
                self.workspace,
                keys,
                ["p2-secret.pdf"],
                project_id="project-one",
            )

        self.assertEqual(records, [])

    def test_project_presentation_rejects_unscoped_parent_for_catalog_bound_foreign_file(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.create_project("项目一", project_id="project-one")
            catalog.create_project("项目二", project_id="project-two")
            asset_id = catalog.create_asset("project-two", "p2-secret", "document")
            catalog.create_asset_version(asset_id, "hash-two", "p2-secret.pdf")
        finally:
            catalog.close()

        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "legacy-unscoped-foreign-parent",
                    "项目二旧索引中的机密材料，不能被项目一按文件名引用。",
                    json.dumps({"source_filename": "p2-secret.pdf"}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(
            collect_indexed_evidence(self.workspace, keys, ["p2-secret.pdf"], project_id="project-one"),
            [],
        )

    def test_global_presentation_rejects_unscoped_parent_for_catalog_bound_project_file(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.create_project("项目二", project_id="project-two")
            asset_id = catalog.create_asset("project-two", "p2-secret", "document")
            catalog.create_asset_version(asset_id, "hash-two", "p2-secret.pdf")
        finally:
            catalog.close()

        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "legacy-unscoped-global-parent",
                    "项目二旧索引中的机密材料，不能被全局 PPT 按文件名引用。",
                    json.dumps({"source_filename": "p2-secret.pdf"}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        assert collect_indexed_evidence(self.workspace, keys, ["p2-secret.pdf"]) == []

    def test_project_presentation_allows_global_evidence_but_not_foreign_project(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.create_project("项目一", project_id="project-one")
        finally:
            catalog.close()
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "global-parent",
                    "全局知识库材料，可被项目一引用。",
                    json.dumps({"source_filename": "shared-guide.pdf"}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        records = collect_indexed_evidence(
            self.workspace,
            keys,
            ["shared-guide.pdf"],
            project_id="project-one",
        )

        self.assertEqual([record["source_filename"] for record in records], ["shared-guide.pdf"])

    def test_global_presentation_does_not_merge_project_bound_same_filename_evidence(self):
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.executemany(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                [
                    (
                        "project-one-report",
                        "项目一的内部报告。",
                        json.dumps({"source_filename": "同名报告.pdf", "project_id": "project-one"}),
                    ),
                    (
                        "project-two-report",
                        "项目二的内部报告。",
                        json.dumps({"source_filename": "同名报告.pdf", "project_id": "project-two"}),
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(
            collect_indexed_evidence(self.workspace, None, ["同名报告.pdf"]),
            [],
        )

    def test_project_presentation_filters_selected_source_version(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.create_project("版本项目", project_id="version-project")
        finally:
            catalog.close()
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.executemany(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                [
                    (
                        "version-one-parent",
                        "第一版材料。",
                        json.dumps({
                            "source_filename": "report.pdf",
                            "project_id": "version-project",
                            "asset_version_id": "version-one",
                        }),
                    ),
                    (
                        "version-two-parent",
                        "第二版材料。",
                        json.dumps({
                            "source_filename": "report.pdf",
                            "project_id": "version-project",
                            "asset_version_id": "version-two",
                        }),
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        records = collect_indexed_evidence(
            self.workspace,
            keys,
            ["report.pdf"],
            project_id="version-project",
            source_version_ids=["version-one"],
        )

        self.assertEqual([record["asset_version_id"] for record in records], ["version-one"])

    def test_verify_pptx_checks_openxml_slide_count(self):
        path = Path(self.temp_dir.name) / "test.pptx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("ppt/presentation.xml", "<p:presentation/>")
            archive.writestr("ppt/slides/slide1.xml", "x" * 9000)

        report = verify_pptx(path, 1)

        self.assertTrue(report["valid"])
        self.assertEqual(report["slide_count"], 1)

    def test_openxml_renderer_writes_editable_utf8_presentation(self):
        path = Path(self.temp_dir.name) / "中文汇报.pptx"
        plan = {
            "title": "数据平台项目汇报",
            "generated_at_label": "2026-08-14",
            "slides": [
                {
                    "kind": "cover",
                    "eyebrow": "项目方案",
                    "title": "数据平台项目汇报",
                    "subtitle": "建设目标与实施路径",
                },
                {
                    "kind": "content",
                    "title": "总体方案",
                    "points": ["建立统一数据平台", "实施分类分级保护"],
                    "sources": ["A方案.pdf", "B政策.pdf"],
                    "footer": "基于已入库文档",
                },
            ],
        }

        result = render_presentation(plan, path)

        self.assertEqual(result["renderer"], "SoulDrive OpenXML")
        self.assertTrue(verify_pptx(path, 2)["valid"])
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            slide_text = archive.read("ppt/slides/slide2.xml").decode("utf-8")
            relationships = archive.read("ppt/_rels/presentation.xml.rels").decode("utf-8")
        self.assertIn("总体方案", slide_text)
        self.assertIn("A方案.pdf", slide_text)
        self.assertIn("ppt/slideMasters/slideMaster1.xml", names)
        self.assertIn('Target="slides/slide2.xml"', relationships)

    def test_service_uses_openxml_renderer_by_default(self):
        service = PresentationService(self.workspace, None)
        inputs = {
            "task_id": "task_12345678",
            "title": "测试汇报",
            "source_filenames": ["A方案.pdf"],
        }
        outputs = {
            "build_outline": {
                "title": "测试汇报",
                "generated_at_label": "2026-08-14",
                "slides": [
                    {"kind": "cover", "title": "测试汇报"},
                    {"kind": "sources", "title": "参考文档", "sources": ["A方案.pdf"]},
                ],
            }
        }

        with patch("core.presentation_service.subprocess.run") as com_renderer:
            result = service._render_pptx_step(inputs, outputs)

        self.assertEqual(result["renderer"], "SoulDrive OpenXML")
        com_renderer.assert_not_called()
        self.assertTrue(verify_pptx(Path(result["output_path"]), 2)["valid"])

        marker = Path(f"{result['output_path']}.souldrive.json")
        marker.unlink()
        original = Path(result["output_path"]).read_bytes()
        with self.assertRaisesRegex(TaskPublicError, "保留|覆盖"):
            service._render_pptx_step(inputs, outputs, {"idempotency_key": "step-render-key"})
        self.assertEqual(Path(result["output_path"]).read_bytes(), original)
        self.assertFalse(marker.exists())

    def _render_recovery_fixture(self):
        inputs = {
            "task_id": "task_content_recovery",
            "title": "Content recovery",
            "source_filenames": ["A.pdf"],
        }
        plan = {
            "title": inputs["title"],
            "slides": [
                {"kind": "cover", "title": inputs["title"]},
                {"kind": "sources", "title": "参考文档", "sources": ["A.pdf"]},
            ],
        }
        output = Path(self.workspace.presentations_path) / presentation_filename(
            inputs["title"], inputs["source_filenames"], inputs["task_id"],
        )
        return inputs, {"build_outline": plan}, output

    def test_render_preserves_existing_output_without_recovery_receipt(self):
        inputs, outputs, output = self._render_recovery_fixture()
        render_presentation(outputs["build_outline"], output)
        original = output.read_bytes()
        with patch.object(presentation_service_module, "render_presentation") as renderer:
            with self.assertRaisesRegex(TaskPublicError, "保留|覆盖"):
                PresentationService(self.workspace, None)._render_pptx_step(inputs, outputs)
        renderer.assert_not_called()
        self.assertEqual(output.read_bytes(), original)

    def test_render_rejects_unproven_output_with_rendering_marker(self):
        inputs, outputs, output = self._render_recovery_fixture()
        render_presentation({**outputs["build_outline"], "title": "另一份报告"}, output)
        original = output.read_bytes()
        marker = Path(f"{output}.souldrive.json")
        _write_json_marker(marker, {
            "task_id": inputs["task_id"], "plan_digest": None,
            "step_idempotency_key": "", "slide_count": 2, "status": "rendering",
        })
        original_marker = marker.read_bytes()
        with patch.object(presentation_service_module, "render_presentation") as renderer:
            with self.assertRaisesRegex(TaskPublicError, "保留|覆盖"):
                PresentationService(self.workspace, None)._render_pptx_step(inputs, outputs)
        renderer.assert_not_called()
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(marker.read_bytes(), original_marker)

    def test_render_preserves_valid_but_externally_edited_output(self):
        inputs, outputs, output = self._render_recovery_fixture()
        service = PresentationService(self.workspace, None)
        service._render_pptx_step(inputs, outputs)
        edited = dict(outputs["build_outline"])
        edited["slides"] = [{"kind": "cover", "title": "用户修改"}, edited["slides"][1]]
        render_presentation(edited, output)
        original = output.read_bytes()
        self.assertTrue(verify_pptx(output, 2)["valid"])
        with patch.object(presentation_service_module, "render_presentation") as renderer:
            with self.assertRaisesRegex(TaskPublicError, "保留|覆盖"):
                service._render_pptx_step(inputs, outputs)
        renderer.assert_not_called()
        self.assertEqual(output.read_bytes(), original)

    def test_render_resumes_ready_staging_file_without_rerendering(self):
        inputs, outputs, output = self._render_recovery_fixture()
        service = PresentationService(self.workspace, None)
        with patch.object(presentation_service_module.os, "link", side_effect=OSError("publish interrupted")):
            with self.assertRaisesRegex(OSError, "publish interrupted"):
                service._render_pptx_step(inputs, outputs)
        self.assertFalse(output.exists())
        marker = json.loads(Path(f"{output}.souldrive.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["status"], "ready")
        with patch.object(presentation_service_module, "render_presentation") as renderer:
            result = service._render_pptx_step(inputs, outputs)
        renderer.assert_not_called()
        self.assertEqual(result["renderer"], "recovered")
        self.assertTrue(verify_pptx(output, 2)["valid"])
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), marker["content_sha256"])

    def test_render_publish_race_never_overwrites_new_destination(self):
        inputs, outputs, output = self._render_recovery_fixture()
        competing_content = b"a user file appeared before publication"

        def publish_race(_source, destination):
            Path(destination).write_bytes(competing_content)
            raise FileExistsError("destination exists")

        with patch.object(presentation_service_module.os, "link", side_effect=publish_race):
            with self.assertRaisesRegex(TaskPublicError, "保留|覆盖"):
                PresentationService(self.workspace, None)._render_pptx_step(inputs, outputs)
        self.assertEqual(output.read_bytes(), competing_content)

    def test_presentation_filenames_bind_full_task_identity(self):
        first = presentation_filename("Report", ["A.pdf"], "task_first_12345678")
        second = presentation_filename("Report", ["A.pdf"], "task_second_12345678")
        self.assertNotEqual(first, second)
        self.assertEqual(first, presentation_filename("Report", ["A.pdf"], "task_first_12345678"))

    def test_render_rejects_changed_staging_bytes_without_publishing(self):
        inputs, outputs, output = self._render_recovery_fixture()
        service = PresentationService(self.workspace, None)
        with patch.object(presentation_service_module.os, "link", side_effect=OSError("publish interrupted")):
            with self.assertRaises(OSError):
                service._render_pptx_step(inputs, outputs)
        marker = json.loads(Path(f"{output}.souldrive.json").read_text(encoding="utf-8"))
        staged = output.parent / marker["staged_name"]
        staged.write_bytes(b"edited staging bytes")
        with patch.object(presentation_service_module, "render_presentation") as renderer:
            with self.assertRaisesRegex(TaskPublicError, "改变|验证"):
                service._render_pptx_step(inputs, outputs)
        renderer.assert_not_called()
        self.assertFalse(output.exists())
        self.assertEqual(staged.read_bytes(), b"edited staging bytes")

    def test_render_preserves_legacy_task_output_and_requires_new_confirmation(self):
        inputs, outputs, output = self._render_recovery_fixture()
        legacy = output.parent / "Content recovery-recovery.pptx"
        render_presentation(outputs["build_outline"], legacy)
        original = legacy.read_bytes()
        _write_json_marker(Path(f"{legacy}.souldrive.json"), {
            "task_id": inputs["task_id"], "plan_digest": None,
            "slide_count": 2, "status": "rendering",
        })
        with patch.object(presentation_service_module, "render_presentation") as renderer:
            with self.assertRaisesRegex(TaskPublicError, "旧版|保留"):
                PresentationService(self.workspace, None)._render_pptx_step(inputs, outputs)
        renderer.assert_not_called()
        self.assertEqual(legacy.read_bytes(), original)
        self.assertFalse(output.exists())

    def test_com_renderer_uses_same_safe_staging_and_content_receipt(self):
        inputs, outputs, output = self._render_recovery_fixture()
        called_paths = []

        def fake_com(command, **kwargs):
            staged = Path(command[command.index("-OutputPath") + 1])
            called_paths.append(staged)
            self.assertNotEqual(staged, output)
            self.assertFalse(output.exists())
            render_presentation(json.loads(kwargs["input"]), staged)
            return SimpleNamespace(returncode=0, stdout='{"renderer":"WPS stub"}', stderr="")

        with patch.dict(presentation_service_module.os.environ, {"SOULDRIVE_PRESENTATION_RENDERER": "com"}):
            with patch.object(presentation_service_module.subprocess, "run", side_effect=fake_com) as renderer:
                result = PresentationService(self.workspace, None)._render_pptx_step(inputs, outputs)
        renderer.assert_called_once()
        self.assertEqual(result["renderer"], "WPS stub")
        self.assertTrue(verify_pptx(output, 2)["valid"])
        self.assertFalse(called_paths[0].exists())
        marker = json.loads(Path(f"{output}.souldrive.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["content_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())

    def test_render_reuses_valid_output_when_final_recovery_marker_write_is_lost(self):
        service = PresentationService(self.workspace, None)
        inputs = {
            "task_id": "task_marker_recovery",
            "title": "Marker recovery",
            "source_filenames": ["A.pdf"],
        }
        outputs = {
            "build_outline": {
                "title": "Marker recovery",
                "generated_at_label": "2026-08-14",
                "slides": [
                    {"kind": "cover", "title": "Marker recovery"},
                    {"kind": "sources", "title": "参考文档", "sources": ["A.pdf"]},
                ],
            }
        }
        real_write_marker = presentation_service_module._write_json_marker

        def write_marker_then_lose_final(path, payload):
            if payload["status"] == "completed":
                raise OSError("final marker write lost")
            return real_write_marker(path, payload)

        with patch.object(
            presentation_service_module,
            "_write_json_marker",
            side_effect=write_marker_then_lose_final,
        ), self.assertRaisesRegex(OSError, "final marker write lost"):
            service._render_pptx_step(inputs, outputs, {"idempotency_key": "marker-retry-001"})

        output_paths = list(Path(self.workspace.presentations_path).glob("Marker recovery-*.pptx"))
        self.assertEqual(len(output_paths), 1)
        output_path = output_paths[0]
        marker_path = Path(f"{output_path}.souldrive.json")
        self.assertTrue(output_path.is_file())
        self.assertTrue(verify_pptx(output_path, 2)["valid"])
        marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(marker_payload["status"], "ready")
        self.assertEqual(marker_payload["content_sha256"], hashlib.sha256(output_path.read_bytes()).hexdigest())

        with patch.object(presentation_service_module, "render_presentation") as rerender:
            recovered = service._render_pptx_step(
                inputs,
                outputs,
                {"idempotency_key": "marker-retry-001"},
            )

        self.assertEqual(recovered["renderer"], "recovered")
        rerender.assert_not_called()
        self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["status"], "completed")

    def test_recovery_marker_keeps_existing_json_when_flush_fails(self):
        marker = Path(self.workspace.runtime_path) / "presentation-marker.json"
        original = b'{"status":"complete"}\n'
        marker.write_bytes(original)
        with patch("core.presentation_service.os.fsync", side_effect=OSError("marker disk full")):
            with self.assertRaisesRegex(OSError, "marker disk full"):
                _write_json_marker(marker, {"status": "replaced"})

        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual(list(marker.parent.glob(f".{marker.name}.*.tmp")), [])

    def test_recovery_marker_keeps_existing_json_when_replace_fails(self):
        marker = Path(self.workspace.runtime_path) / "presentation-marker-replace.json"
        original = b'{"status":"complete"}\n'
        marker.write_bytes(original)
        with patch("core.presentation_service.os.replace", side_effect=OSError("marker replace denied")):
            with self.assertRaisesRegex(OSError, "marker replace denied"):
                _write_json_marker(marker, {"status": "replaced"})

        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual(list(marker.parent.glob(f".{marker.name}.*.tmp")), [])

    def test_run_result_reports_extractable_outline_evidence_and_timing(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                ("p1", "系统通过本地证据链生成科研汇报，并保留文档来源。", json.dumps({"source_filename": "A方案.pdf"})),
            )
            conn.commit()
        finally:
            conn.close()
        service = PresentationService(self.workspace, keys)
        plan_task = service.create_plan({"source_filenames": ["A方案.pdf"], "open_in_wps": False})
        planned = service.run_task(plan_task["task_id"])
        generation_task = service.confirm_plan(
            plan_task["task_id"],
            planned["result"]["plan_digest"],
        )
        result = service.run_task(generation_task["task_id"])["result"]

        self.assertEqual(result["outline_source"], "extractive")
        self.assertEqual(result["evidence_metrics"]["evidence_slide_coverage"], 1.0)
        self.assertEqual(result["evidence_metrics"]["selected_source_coverage"], 1.0)
        self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_presentation_requires_exact_outline_confirmation_before_render(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                ("p1", "系统保留大纲证据并生成可编辑演示文稿。", json.dumps({"source_filename": "A方案.pdf"})),
            )
            conn.commit()
        finally:
            conn.close()

        service = PresentationService(self.workspace, keys)
        plan_task = service.create_plan({
            "request_id": "request-outline-confirmation",
            "source_filenames": ["A方案.pdf"],
            "open_in_wps": False,
        })
        planned = service.run_task(plan_task["task_id"])
        public_plan = service.get_task(plan_task["task_id"])

        self.assertEqual(planned["status"], "completed")
        self.assertIsNotNone(planned["result"]["plan_digest"])
        self.assertIn("outline", public_plan["result"])
        self.assertNotIn("evidence_snapshot", public_plan["result"])
        self.assertEqual(list(Path(self.workspace.presentations_path).glob("*.pptx")), [])

        generation_task = service.confirm_plan(
            plan_task["task_id"],
            planned["result"]["plan_digest"],
        )
        same_generation = service.confirm_plan(
            plan_task["task_id"],
            planned["result"]["plan_digest"],
        )
        self.assertEqual(generation_task["task_id"], same_generation["task_id"])

        completed = service.run_task(generation_task["task_id"])
        self.assertEqual(completed["status"], "completed")
        output_path = Path(completed["result"]["output_path"])
        self.assertTrue(output_path.is_file())
        self.assertTrue(Path(f"{output_path}.souldrive.json").is_file())

        with self.assertRaisesRegex(ValueError, "request_id"):
            service.create_plan({
                "request_id": "request-outline-confirmation",
                "source_filenames": ["A方案.pdf"],
                "title": "另一份汇报",
                "open_in_wps": False,
            })

    def test_confirm_retry_repairs_artifact_after_generation_task_was_durable_first(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.create_project("确认恢复项目", project_id="confirm-recovery")
        finally:
            catalog.close()
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "confirm-recovery-parent",
                    "项目证据需要在确认恢复后继续生成。",
                    json.dumps({"source_filename": "确认恢复.pdf", "project_id": "confirm-recovery"}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        service = PresentationService(self.workspace, keys)
        plan_task = service.create_plan({
            "project_id": "confirm-recovery",
            "request_id": "confirm-recovery-request",
            "source_filenames": ["确认恢复.pdf"],
            "open_in_wps": False,
        })
        planned = service.run_task(plan_task["task_id"])

        with patch.object(ProjectCatalog, "set_artifact_status", side_effect=RuntimeError("simulated catalog interruption")):
            with self.assertRaisesRegex(RuntimeError, "catalog interruption"):
                service.confirm_plan(plan_task["task_id"], planned["result"]["plan_digest"])

        generation_task = service.confirm_plan(plan_task["task_id"], planned["result"]["plan_digest"])
        artifact = ProjectCatalog(self.workspace)
        try:
            stored = artifact.get_artifact(plan_task["inputs"]["artifact_id"])
        finally:
            artifact.close()

        self.assertEqual(stored["confirmation_status"], "confirmed")
        self.assertEqual(generation_task["status"], "pending")
        completed = service.run_task(generation_task["task_id"])
        self.assertEqual(completed["status"], "completed")

    def test_completed_plan_replay_repairs_missing_artifact_projection(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.create_project("计划投影恢复项目", project_id="plan-projection-recovery")
        finally:
            catalog.close()
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)"
            )
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "plan-projection-parent",
                    "计划完成后即使投影短暂失败，重放也必须恢复大纲状态。",
                    json.dumps({
                        "source_filename": "计划恢复.pdf",
                        "project_id": "plan-projection-recovery",
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        service = PresentationService(self.workspace, keys)
        plan_task = service.create_plan({
            "project_id": "plan-projection-recovery",
            "request_id": "plan-projection-recovery-request",
            "source_filenames": ["计划恢复.pdf"],
            "open_in_wps": False,
        })
        with patch.object(service, "_sync_presentation_artifact"):
            planned = service.run_task(plan_task["task_id"])
        self.assertEqual(planned["status"], "completed")

        catalog = ProjectCatalog(self.workspace)
        try:
            before = catalog.get_artifact(plan_task["inputs"]["artifact_id"])
        finally:
            catalog.close()
        self.assertEqual(before["confirmation_record"]["generation_status"], "not_started")

        replayed = service.run_task(plan_task["task_id"])
        self.assertEqual(replayed["status"], "completed")
        catalog = ProjectCatalog(self.workspace)
        try:
            after = catalog.get_artifact(plan_task["inputs"]["artifact_id"])
        finally:
            catalog.close()
        self.assertEqual(after["confirmation_record"]["generation_status"], "plan_ready")

    def test_legacy_unconfirmed_generation_task_cannot_resume(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        journal = TaskJournal(self.workspace, keys)
        try:
            legacy = journal.create_task(
                "presentation.generate",
                {"source_filenames": ["A方案.pdf"], "task_id": "legacy-task"},
                ("collect_evidence", "build_outline", "render_pptx", "verify_pptx", "open_in_wps"),
                task_id="legacy-task",
            )
        finally:
            journal.close()
        service = PresentationService(self.workspace, keys)
        with self.assertRaisesRegex(ValueError, "旧 PPT 任务"):
            service.resume_task(legacy["task_id"])

    def test_project_presentation_records_versions_plan_and_completion_provenance(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.create_project("论文项目", project_id="paper")
            asset_id = catalog.create_asset("paper", "A 方案", "document", asset_id="asset-a")
            version_id = catalog.create_asset_version(asset_id, "hash-a", "A方案.pdf", version_id="version-a")
        finally:
            catalog.close()
        db_path = Path(self.workspace.parent_doc_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute(
                "INSERT INTO parent_documents VALUES (?, ?, ?)",
                (
                    "p1",
                    "系统保留来源版本并生成可追溯汇报。",
                    json.dumps({
                        "source_filename": "A方案.pdf",
                        "project_id": "paper",
                        "asset_version_id": version_id,
                        "asset_id": asset_id,
                        "section": "方案",
                    }),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        service = PresentationService(self.workspace, keys)
        plan_task = service.create_plan({
            "project_id": "paper",
            "source_filenames": ["A方案.pdf"],
            "open_in_wps": False,
            "title": "方案汇报",
        })
        artifact_id = plan_task["inputs"]["artifact_id"]
        planned = service.run_task(plan_task["task_id"])
        generation_task = service.confirm_plan(
            plan_task["task_id"],
            planned["result"]["plan_digest"],
        )
        result = service.run_task(generation_task["task_id"])
        artifact = ProjectCatalog(self.workspace)
        try:
            stored = artifact.get_artifact(artifact_id)
        finally:
            artifact.close()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["artifact_id"], artifact_id)
        self.assertEqual(result["result"]["source_version_ids"], [version_id])
        self.assertEqual(stored["source_version_ids"], [version_id])
        self.assertEqual(stored["plan"]["task_id"], plan_task["task_id"])
        self.assertEqual(stored["plan"]["plan_task_id"], plan_task["task_id"])
        self.assertEqual(stored["confirmation_record"]["generation_status"], "completed")
        self.assertIn("生成结果", stored["change_summary"])

    def test_custom_outline_generator_is_identified_without_claiming_ollama(self):
        service = PresentationService(
            self.workspace,
            None,
            outline_generator=lambda _records, _inputs: {
                "title": "自定义",
                "slides": [{"kind": "cover", "title": "自定义"}, {"kind": "sources", "title": "来源", "sources": ["A方案.pdf"]}],
            },
        )
        result = service._build_outline_step(
            validate_presentation_request({"source_filenames": ["A方案.pdf"], "max_slides": 4, "title": "自定义"}),
            {"collect_evidence": {"records": []}},
        )

        self.assertEqual(result["outline_source"], "custom_generator")

    def test_generated_outline_rejects_unbound_claims_before_review(self):
        inputs = validate_presentation_request({"source_filenames": ["old.txt", "new.txt"], "title": "资料核对"})
        records = [
            {"source_filename": "old.txt", "section": "记录", "content": "独立工程样例的指标甲记录值为0.40，数据版本为demo-v1。"},
            {"source_filename": "new.txt", "section": "记录", "content": "独立工程样例的指标甲记录值为0.55，未记录数据版本。"},
        ]
        cases = (
            {"points": ["指标甲记录值为0.99。"]},
            {"points": [records[1]["content"]], "sources": ["old.txt"]},
            {"points": ["配置变更显著提升指标甲，证明优化有效。"]},
            {"points": ["数据版本保持demo-v1不变。"]},
            {"title": "指标甲显著提升"},
            {"subtitle": "配置变更证明优化有效"},
            {"footer": "数据版本完全一致"},
            {"points": ["记录值为0.55。"], "sources": ["not-selected.txt"]},
            {"points": ["独立工程样例的指标甲记录值为0.5，未记录数据版本。"], "sources": ["new.txt"]},
            {"points": ["独立工程样例的指标甲记录值为0.55%，未记录数据版本。"], "sources": ["new.txt"]},
        )
        for changed in cases:
            with self.subTest(changed=changed):
                slide = {"kind": "content", "title": "资料摘录", "points": [records[0]["content"]], "sources": ["old.txt"], **changed}
                proposal = {"title": "资料核对", "outline_source": "local_ollama", "slides": [
                    {"kind": "cover", "title": "资料核对"}, slide,
                    {"kind": "sources", "title": "资料来源", "sources": inputs["source_filenames"]},
                ]}
                service = PresentationService(self.workspace, None, outline_generator=lambda _records, _inputs: proposal)
                result = service._build_outline_step(inputs, {"collect_evidence": {"records": records}})
                self.assertEqual(result["outline_source"], "extractive")
                self.assertEqual(result["evidence_validation"]["status"], "extractive_fallback")
                self.assertIn("原文", result["evidence_validation"]["notice"])
                self.assertNotIn("显著提升", json.dumps(result, ensure_ascii=False))
                self.assertNotIn("0.99", json.dumps(result, ensure_ascii=False))
                self.assertIn("不据此判定可比性或因果", result["slides"][0]["subtitle"])
                self.assertTrue(all("原文摘录" in slide["footer"] for slide in result["slides"] if slide["kind"] == "content"))

    def test_generated_outline_accepts_source_bound_excerpts(self):
        inputs = validate_presentation_request({"source_filenames": ["source.txt"], "title": "资料核对"})
        record = {"source_filename": "source.txt", "section": "记录", "content": "本地资料保留原始证据，记录值为0.55。"}
        proposal = {"title": "资料核对", "outline_source": "local_ollama", "slides": [
            {"kind": "cover", "title": "资料核对"},
            {"kind": "content", "title": "资料摘录", "points": [record["content"]], "sources": ["source.txt"]},
            {"kind": "sources", "title": "资料来源", "sources": ["source.txt"]},
        ]}
        service = PresentationService(self.workspace, None, outline_generator=lambda _records, _inputs: proposal)
        result = service._build_outline_step(inputs, {"collect_evidence": {"records": [record]}})
        self.assertEqual(result["outline_source"], "local_ollama")
        self.assertEqual(result["evidence_validation"]["status"], "source_bound_excerpts")
        self.assertIn("不证明", result["evidence_validation"]["notice"])

    def test_legacy_document_outline_cannot_bypass_confirm_or_resume_guard(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        conn = sqlite3.connect(self.workspace.parent_doc_path)
        try:
            conn.execute("CREATE TABLE parent_documents (parent_id TEXT PRIMARY KEY, content TEXT, metadata_json TEXT)")
            conn.execute("INSERT INTO parent_documents VALUES (?, ?, ?)", (
                "engineering-record", "工程资料记录值为0.55，未证明配置变更改善结果。",
                json.dumps({"source_filename": "record.txt"}),
            ))
            conn.commit()
        finally:
            conn.close()
        service = PresentationService(self.workspace, keys)
        inputs = validate_presentation_request({"source_filenames": ["record.txt"], "title": "资料核对", "open_in_wps": False})
        legacy_outline = presentation_service_module.validate_presentation_plan({"title": "资料核对", "slides": [
            {"kind": "cover", "title": "资料核对"},
            {"kind": "content", "title": "资料摘录", "points": ["配置变更改善结果。"], "sources": ["record.txt"]},
        ]}, inputs)
        legacy_outline["outline_source"] = "local_ollama"
        # Reproduce an older persisted proposal without mutating the real fixture.
        with patch.object(service, "_build_outline_step", return_value=legacy_outline):
            planned = service.run_task(service.create_plan(inputs)["task_id"])
        self.assertEqual(planned["status"], "completed")
        with self.assertRaisesRegex(ValueError, "原文绑定.*重新"):
            service.confirm_plan(planned["task_id"], planned["result"]["plan_digest"])
        with patch.object(presentation_service_module, "_require_generated_outline_evidence"):
            generation = service.confirm_plan(planned["task_id"], planned["result"]["plan_digest"])
        journal = TaskJournal(self.workspace, keys)
        try:
            generation_inputs = journal.get_task(generation["task_id"])["inputs"]
        finally:
            journal.close()
        with self.assertRaisesRegex(ValueError, "原文绑定.*重新"):
            service._load_confirmed_plan_step(generation_inputs, {})
        self.assertEqual(list(Path(self.workspace.presentations_path).glob("*.pptx")), [])

    def test_extractive_summary_and_stages_do_not_lose_their_actual_source_labels(self):
        sources = [f"source-{index}.txt" for index in range(6)]
        records = [{"source_filename": source, "section": "记录", "content": (
            "" if index == 0 else f"工程资料{index}保留独立原始记录，不能借用其他文件的标签。"
        )} for index, source in enumerate(sources)]
        inputs = validate_presentation_request({"source_filenames": sources, "title": "资料核对", "max_slides": 16})
        plan = build_extractable_presentation_plan(records, inputs)
        summary = plan["slides"][1]
        self.assertEqual(summary["sources"], sources[1:5])
        self.assertEqual(len(summary["points"]), 4)
        for point in summary["points"]:
            self.assertTrue(any(point.startswith(Path(source).stem + "：") for source in summary["sources"]))
        stages = presentation_service_module._skill_stage_slides(records, sources,
            {"section_sequence": ["记录"], "stage_keywords": [["工程资料5"]]}, 1, 5, "")
        self.assertNotIn("工程资料5", " ".join(stages[0]["points"]))
        self.assertEqual(stages[0]["sources"], sources[:3])

    def test_presentation_evidence_preserves_structured_lines(self):
        content = "| metric | value |\n| --- | --- |\n| alpha | 0.40 |\n| beta | -0.55 |\n| latency | 1234 |"
        record = presentation_service_module._evidence_record("table-record", content, {"source_filename": "metrics.csv"})
        self.assertEqual(record["content"], content)

    def test_extractive_table_keeps_each_header_value_pair_without_ellipsis(self):
        content = "| metric | value |\n| --- | --- |\n| alpha | 0.40 |\n| beta | -0.55 |\n| latency | 1234 |"
        record = presentation_service_module._evidence_record("table-record", content, {"source_filename": "metrics.csv"})
        plan = build_extractable_presentation_plan([record], validate_presentation_request({"source_filenames": ["metrics.csv"]}))
        slide = next(slide for slide in plan["slides"] if slide["kind"] == "content" and slide["title"] == "metrics")
        self.assertEqual(slide["points"], ["metric：alpha；value：0.40", "metric：beta；value：-0.55", "metric：latency；value：1234"])
        self.assertNotIn("…", " ".join(slide["points"]))

    def test_structured_table_never_silently_cuts_a_long_value(self):
        record = {"source_filename": "metrics.csv", "section": "记录", "content": (
            "| metric | value |\n| --- | --- |\n| alpha | " + "long-value-" * 15 + " |"
        )}
        with self.assertRaisesRegex(TaskPublicError, "表格.*过长"):
            build_extractable_presentation_plan([record], validate_presentation_request({"source_filenames": ["metrics.csv"]}))

    def test_table_extraction_keeps_empty_cells_and_escaped_pipes_without_inferring_units(self):
        content = "| name | value | unit |\n| --- | --- | --- |\n| alpha\\|beta | 0.55 | |"
        self.assertEqual(presentation_service_module._presentation_table_points(content),
                         ["name：alpha|beta；value：0.55；unit：（原文空白）"])
        self.assertIsNone(presentation_service_module._presentation_table_points("| 非表格段落 |\n另一条普通正文。"))
        with self.assertRaisesRegex(TaskPublicError, "表格列数"):
            presentation_service_module._presentation_table_points("| name | value |\n| --- | --- |\n| alpha |")

    def test_template_skill_is_bound_to_task_and_changes_safe_palette(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        service = PresentationService(
            self.workspace,
            keys,
            style_skill_lookup=lambda skill_id: {
                "skill_id": skill_id,
                "title": "学院蓝组会模板",
                "presentation_style": {"primary": "123456", "accent": "FEDCBA"},
            } if skill_id == "skill_template" else None,
        )
        task = service.create_task({
            "source_filenames": ["A方案.pdf"],
            "style_skill_id": "skill_template",
        })
        self.assertEqual(task["inputs"]["style_skill"]["skill_id"], "skill_template")
        plan = build_extractable_presentation_plan([], task["inputs"])
        self.assertEqual(plan["style"]["primary"], "123456")
        self.assertEqual(plan["style"]["accent"], "FEDCBA")

    def test_unknown_template_skill_is_rejected(self):
        service = PresentationService(self.workspace, None, style_skill_lookup=lambda _skill_id: None)
        with self.assertRaisesRegex(ValueError, "风格 Skill"):
            service.create_task({"source_filenames": ["A方案.pdf"], "style_skill_id": "missing"})

    def test_skill_without_declarative_style_uses_safe_default(self):
        skill = {
            "skill_id": "skill_frontend",
            "title": "frontend-slides",
            "content": "Create animation-rich HTML presentations from scratch.",
        }
        plan = build_extractable_presentation_plan(
            [],
            {
                "source_filenames": ["A方案.pdf"],
                "title": "Skill deck",
                "audience": "research",
                "max_slides": 6,
                "style_skill": skill,
            },
        )

        self.assertEqual(plan["style"]["primary"], "173F3B")
        self.assertEqual(plan["style"]["content_variant"], "evidence_split")
        self.assertFalse(plan["skill_application"]["style_applied"])
        self.assertTrue(plan["skill_application"]["instructions_applied"])
        self.assertEqual(plan["skill_application"]["skill_id"], "skill_frontend")
        self.assertEqual(plan["presentation_contract"]["adaptation"], "native_pptx")

    def test_selected_skill_changes_deterministic_business_contract(self):
        records = [
            {"source_filename": "A.pdf", "content": "第一条结论。", "section": "结论", "page_number": 1},
        ]
        default_plan = build_extractable_presentation_plan(records, {
            "source_filenames": ["A.pdf"], "title": "汇报", "audience": "research", "max_slides": 6,
        })
        skill_plan = build_extractable_presentation_plan(records, {
            "source_filenames": ["A.pdf"], "title": "汇报", "audience": "research", "max_slides": 6,
            "style_skill": {"skill_id": "pptx", "title": "pptx", "content": "Create editable PPTX slides."},
        })
        self.assertEqual(default_plan["presentation_contract"]["adaptation"], "system_default")
        self.assertEqual(skill_plan["presentation_contract"]["adaptation"], "native_pptx")
        self.assertNotEqual(default_plan["slides"][0]["subtitle"], skill_plan["slides"][0]["subtitle"])
        self.assertLessEqual(len(skill_plan["slides"][1]["points"]), 4)

    def test_selected_skill_validation_script_is_ignored(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        skill_dir = Path(self.workspace.skills_path) / "ppt-check"
        script = skill_dir / "scripts" / "validate.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            "import json, pathlib, sys\n"
            "pptx = pathlib.Path(sys.argv[1])\n"
            "assert pptx.is_file()\n"
            "print(json.dumps({'checked': pptx.name}))\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            "---\nname: PPT Check\ndescription: Validate PPTX\n---\n",
            encoding="utf-8",
        )
        skill = {
            "skill_id": "skill_ppt_check",
            "title": "PPT Check",
            "source_path": "ppt-check/SKILL.md",
            "resource_manifest": [{
                "path": "scripts/validate.py",
                "kind": "script",
                "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            }],
        }
        service = PresentationService(self.workspace, keys)
        output_path = Path(self.workspace.presentations_path) / "input.pptx"
        render_presentation(
            {"title": "输入", "slides": [{"kind": "cover", "title": "输入"}, {"kind": "sources", "title": "来源", "sources": ["A.pdf"]}]},
            output_path,
        )

        result = service._verify_pptx_step(
            {"style_skill": skill},
            {"render_pptx": {"output_path": str(output_path), "planned_slide_count": 2}},
        )

        self.assertTrue(result["valid"])
        self.assertIsNone(result["skill_execution"])
        self.assertTrue(result["skill_gate_passed"])

    def test_failed_selected_skill_validation_script_is_not_executed(self):
        initialize_keystore(self.workspace, "correct horse battery staple")
        keys = unlock_keystore(self.workspace, "correct horse battery staple")
        skill_dir = Path(self.workspace.skills_path) / "ppt-check-fail"
        script = skill_dir / "scripts" / "validate.py"
        script.parent.mkdir(parents=True)
        script.write_text("raise SystemExit('style contract rejected')\n", encoding="utf-8")
        (skill_dir / "SKILL.md").write_text(
            "---\nname: PPT Check Fail\ndescription: Reject PPTX\n---\n",
            encoding="utf-8",
        )
        skill = {
            "skill_id": "skill_ppt_check_fail",
            "title": "PPT Check Fail",
            "source_path": "ppt-check-fail/SKILL.md",
            "resource_manifest": [{
                "path": "scripts/validate.py",
                "kind": "script",
                "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            }],
        }
        service = PresentationService(self.workspace, keys)
        output_path = Path(self.workspace.presentations_path) / "input-fail.pptx"
        render_presentation(
            {"title": "输入", "slides": [{"kind": "cover", "title": "输入"}, {"kind": "sources", "title": "来源", "sources": ["A.pdf"]}]},
            output_path,
        )

        result = service._verify_pptx_step(
            {"style_skill": skill},
            {"render_pptx": {"output_path": str(output_path), "planned_slide_count": 2}},
        )
        self.assertTrue(result["valid"])
        self.assertIsNone(result["skill_execution"])

    def test_declarative_skill_style_changes_layout_and_font(self):
        skill = {
            "skill_id": "skill_frontend",
            "title": "frontend-slides",
            "content": "Create animation-rich HTML presentations from scratch.",
            "presentation_style_source": "presentation_style.json",
            "presentation_style": {
                "primary": "102A43",
                "accent": "F97316",
                "background": "F7F3EA",
                "text": "243B53",
                "surface": "FFFDF7",
                "cover_variant": "editorial_light",
                "content_variant": "split_cards",
                "font_family": "Arial",
            },
            "resource_manifest": [
                {"path": "scripts/export-presentation.ps1", "kind": "script"},
                {"path": "STYLE_PRESETS.md", "kind": "instruction"},
            ],
        }
        plan = build_extractable_presentation_plan(
            [],
            {
                "source_filenames": ["A方案.pdf"],
                "title": "Skill deck",
                "audience": "research",
                "max_slides": 6,
                "style_skill": skill,
            },
        )
        default_plan = build_extractable_presentation_plan(
            [],
            {
                "source_filenames": ["A方案.pdf"],
                "title": "Skill deck",
                "audience": "research",
                "max_slides": 6,
            },
        )
        styled_path = Path(self.temp_dir.name) / "styled.pptx"
        default_path = Path(self.temp_dir.name) / "default.pptx"
        render_presentation(plan, styled_path)
        render_presentation(default_plan, default_path)

        with zipfile.ZipFile(styled_path) as archive:
            styled_cover = archive.read("ppt/slides/slide1.xml").decode("utf-8")
            styled_content = archive.read("ppt/slides/slide2.xml").decode("utf-8")
        with zipfile.ZipFile(default_path) as archive:
            default_cover = archive.read("ppt/slides/slide1.xml").decode("utf-8")
            default_content = archive.read("ppt/slides/slide2.xml").decode("utf-8")

        self.assertEqual(plan["style"]["content_variant"], "split_cards")
        self.assertTrue(plan["skill_application"]["style_applied"])
        self.assertEqual(plan["skill_application"]["script_count"], 0)
        self.assertIn('typeface="Arial"', styled_cover)
        self.assertIn('val="102A43"', styled_cover)
        self.assertNotEqual(styled_cover, default_cover)
        self.assertNotEqual(styled_content, default_content)


if __name__ == "__main__":
    unittest.main()
