import pytest
import tempfile
from types import SimpleNamespace
from fastapi.testclient import TestClient

from core.multimodal_workflows import (
    build_workflow_query,
    evaluate_workflow_output_contract,
    list_workflow_specs,
    plan_workflow,
    validate_workflow_citation_namespaces,
    workflow_retrieval_policy,
    workflow_retrieval_variants,
)
from core import mcp_server
from core.enterprise_security import SlidingWindowRateLimiter
from core.project_catalog import ProjectCatalog
from core.workspace import SoulDriveWorkspace


def test_catalog_exposes_three_business_first_workflows():
    specs = list_workflow_specs()
    assert {item["workflow_id"] for item in specs} == {
        "paper_review",
        "experiment_compare",
        "meeting_actions",
    }
    assert all(item["output"] for item in specs)
    assert all(item["playbook_version"] == "1.0" for item in specs)
    assert all(item["input_contract"] and item["side_effects"] for item in specs)
    assert all(item["approval"]["status"] == "unreviewed" for item in specs)
    assert all(item["response_contract"]["required_sections"] for item in specs)
    assert all(item["response_contract"]["missing_evidence_marker"] == "待确认" for item in specs)
    assert all(item["response_contract"]["allowed_citation_prefixes"] for item in specs)


def test_workflow_retrieval_variants_make_research_windows_behaviorally_distinct():
    paper_variants = workflow_retrieval_variants("paper_review", "总结这篇论文")
    experiment_variants = workflow_retrieval_variants("experiment_compare", "分析这次实验")

    assert workflow_retrieval_policy("paper_review")["profile"] == "paper_review"
    assert workflow_retrieval_policy("experiment_compare")["profile"] == "experiment_compare"
    assert paper_variants
    assert experiment_variants
    assert paper_variants != experiment_variants
    assert any("研究问题" in item and "核心方法" in item for item in paper_variants)
    assert any("指标" in item and "数据集" in item for item in experiment_variants)
    assert workflow_retrieval_variants(None, "普通问题") == []


def test_workflow_citation_namespaces_block_cross_playbook_evidence_types():
    accepted = validate_workflow_citation_namespaces(
        "paper_review",
        "论文结论来自文档。[E1]\n截图补充显示当前页面。[V1]",
    )
    rejected = validate_workflow_citation_namespaces(
        "paper_review",
        "论文准确率为 85%。[M1]",
    )

    assert accepted["valid"] is True
    assert rejected["valid"] is False
    assert rejected["disallowed_citations"] == ["M1"]
    assert rejected["allowed_citation_prefixes"] == ["E", "V"]


def test_workflow_output_contract_detects_missing_sections_without_judging_facts():
    result = evaluate_workflow_output_contract(
        "experiment_compare",
        "## 结论\n- accuracy 下降。[M1]\n## 已证实变化\n- 已确认。 [M1]",
    )

    assert result["status"] == "incomplete"
    assert result["observed_sections"] == ["结论", "已证实变化"]
    assert result["missing_sections"] == ["高可信候选原因", "待验证假设", "最小验证实验"]


def test_workflow_output_contract_accepts_numbered_and_colon_headings():
    result = evaluate_workflow_output_contract(
        "meeting_actions",
        "1. 会议结论：已确认方向\n2. 行动项：待确认\n3. 待确认信息：无\n4. 草稿边界：仅生成本地草稿",
    )

    assert result["status"] == "pass"
    assert result["missing_sections"] == []


def test_workflow_output_contract_rejects_empty_required_sections():
    result = evaluate_workflow_output_contract(
        "paper_review",
        "## 研究问题\n论文研究 RAG。[E1]\n## 核心方法\n## 实验结果\n"
        "## 局限与复现\n待确认\n## 汇报提纲\n待确认",
    )

    assert result["status"] == "incomplete"
    assert result["empty_sections"] == ["核心方法", "实验结果"]
    assert result["missing_sections"] == ["核心方法", "实验结果"]


def test_paper_review_requires_a_project_and_indexed_document():
    plan = plan_workflow("paper_review", document_names=[])
    assert plan["ready"] is False
    assert "请先选择项目空间" in plan["missing_inputs"]
    assert "至少选择一篇已索引论文" in plan["missing_inputs"]

    ready = plan_workflow("paper_review", project_id="p1", document_names=["论文A.pdf"])
    assert ready["ready"] is True
    assert ready["evidence_scope"] == ["论文A.pdf"]
    assert ready["routing"]["needs_confirmation"] is True
    assert ready["response_contract"]["required_sections"][:2] == ["研究问题", "核心方法"]


def test_workflow_plan_requires_current_project_playbook_approval_when_enabled():
    blocked = plan_workflow(
        "paper_review",
        project_id="p1",
        document_names=["论文A.pdf"],
        require_approval=True,
    )
    assert blocked["ready"] is False
    assert blocked["approval"]["status"] == "unreviewed"
    assert "审核通过" in blocked["next_action"]

    ready = plan_workflow(
        "paper_review",
        project_id="p1",
        document_names=["论文A.pdf"],
        approval={"status": "approved", "playbook_version": "1.0"},
        require_approval=True,
    )
    assert ready["ready"] is True
    assert ready["approval"]["status"] == "approved"


def test_experiment_compare_accepts_two_documents_or_an_authorized_screen():
    missing = plan_workflow("experiment_compare", project_id="p1", document_names=["实验A.pdf"])
    assert missing["ready"] is False

    screen = plan_workflow("experiment_compare", project_id="p1", has_image=True)
    assert screen["ready"] is True
    assert screen["routing"]["visual_mode"] == "slow"
    assert "本轮授权屏幕选区" in screen["evidence_scope"]

    docs = plan_workflow("experiment_compare", project_id="p1", document_names=["A.pdf", "B.pdf"])
    assert docs["ready"] is True

    ledger = plan_workflow(
        "experiment_compare",
        project_id="p1",
        baseline_run_id="run-baseline",
        current_run_id="run-current",
    )
    assert ledger["ready"] is True
    assert ledger["input_state"]["has_experiment_comparison"] is True
    assert "结构化实验对比（M#）" in ledger["evidence_scope"]

    partial = plan_workflow(
        "experiment_compare",
        project_id="p1",
        baseline_run_id="run-baseline",
    )
    assert partial["ready"] is False
    assert "请同时选择基线运行和当前运行" in partial["missing_inputs"]


def test_meeting_workflow_requires_confirmable_speech_source():
    missing = plan_workflow("meeting_actions", project_id="p1")
    assert missing["ready"] is False
    assert "语音录音" in missing["next_action"]

    ready = plan_workflow("meeting_actions", project_id="p1", has_audio=True)
    assert ready["ready"] is True
    assert ready["routing"]["needs_rag"] is False

    text_ready = plan_workflow(
        "meeting_actions",
        project_id="p1",
        text_note="周五前确认实验报告负责人。",
    )
    assert text_ready["ready"] is True
    assert text_ready["input_state"]["has_text_note"] is True
    assert "本轮用户提供的会议/阅读记录文本" in text_ready["evidence_scope"]


def test_workflow_query_keeps_original_request_and_adds_evidence_contract():
    query = build_workflow_query("paper_review", "总结这三篇论文")
    assert "论文精读" in query
    assert "仅使用可定位证据" in query
    assert "总结这三篇论文" in query


def test_unknown_workflow_is_rejected():
    with pytest.raises(ValueError):
        plan_workflow("not-a-workflow", project_id="p1")
    with pytest.raises(ValueError):
        build_workflow_query("not-a-workflow", "任务")


def test_workflow_catalog_rejects_unknown_project_scope(tmp_path, monkeypatch):
    workspace = SoulDriveWorkspace.from_drive(str(tmp_path)).ensure()
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-scope-token")

    response = TestClient(mcp_server.app).get(
        "/multimodal/workflows",
        params={"project_id": "project-does-not-exist"},
        headers={"X-SoulDrive-Token": "workflow-scope-token"},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "unknown project"


def test_workflow_plan_rejects_unknown_project_scope(tmp_path, monkeypatch):
    workspace = SoulDriveWorkspace.from_drive(str(tmp_path)).ensure()
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-plan-scope-token")

    response = TestClient(mcp_server.app).post(
        "/multimodal/workflows/plan",
        headers={"X-SoulDrive-Token": "workflow-plan-scope-token"},
        json={"workflow_id": "paper_review", "project_id": "project-does-not-exist"},
    )

    assert response.status_code == 404
    assert response.json()["error"] == "unknown project"


def test_workflow_plan_api_returns_input_gap_without_loading_models(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "_project_exists", lambda *_args: True)
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-token")

    client = TestClient(mcp_server.app)
    response = client.post(
        "/multimodal/workflows/plan",
        headers={"X-SoulDrive-Token": "workflow-token"},
        json={"workflow_id": "paper_review", "project_id": "p1"},
    )

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert "至少选择一篇已索引论文" in response.json()["missing_inputs"]


def test_workflow_plan_api_verifies_global_and_foreign_document_scope(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: SimpleNamespace(root_path="test-workspace"))
    monkeypatch.setattr(mcp_server, "_project_exists", lambda *_args: True)
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_document_library_payload",
        lambda _workspace: {
            "documents": [
                {
                    "name": "共享规范.pdf",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "global",
                    "project_ids": [],
                },
                {
                    "name": "其他项目.pdf",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "project",
                    "project_ids": ["p2"],
                },
            ]
        },
    )
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-plan-document-scope-token")
    client = TestClient(mcp_server.app)

    shared = client.post(
        "/multimodal/workflows/plan",
        headers={"X-SoulDrive-Token": "workflow-plan-document-scope-token"},
        json={
            "workflow_id": "paper_review",
            "project_id": "p1",
            "document_names": ["共享规范.pdf"],
        },
    )
    assert shared.status_code == 200
    assert shared.json()["ready"] is True

    foreign = client.post(
        "/multimodal/workflows/plan",
        headers={"X-SoulDrive-Token": "workflow-plan-document-scope-token"},
        json={
            "workflow_id": "paper_review",
            "project_id": "p1",
            "document_names": ["其他项目.pdf"],
        },
    )
    assert foreign.status_code == 200
    assert foreign.json()["ready"] is False
    assert "不属于当前项目" in foreign.json()["missing_inputs"][0]


def test_workflow_plan_api_uses_document_id_for_duplicate_names(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: SimpleNamespace(root_path="test-workspace"))
    monkeypatch.setattr(mcp_server, "_project_exists", lambda *_args: True)
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_document_library_payload",
        lambda _workspace: {
            "documents": [
                {
                    "name": "同名论文.pdf",
                    "document_id": "object-b",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "project",
                    "project_ids": ["p2"],
                },
                {
                    "name": "同名论文.pdf",
                    "document_id": "object-a",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "project",
                    "project_ids": ["p1"],
                },
            ]
        },
    )
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-document-id-token")

    response = TestClient(mcp_server.app).post(
        "/multimodal/workflows/plan",
        headers={"X-SoulDrive-Token": "workflow-document-id-token"},
        json={
            "workflow_id": "paper_review",
            "project_id": "p2",
            "document_names": ["同名论文.pdf"],
            "document_ids": ["object-b"],
        },
    )

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["input_state"]["document_id_count"] == 1


def test_workflow_plan_rejects_name_only_duplicate_documents(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: SimpleNamespace(root_path="test-workspace"))
    monkeypatch.setattr(mcp_server, "_project_exists", lambda *_args: True)
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_document_library_payload",
        lambda _workspace: {
            "documents": [
                {
                    "name": "同名论文.pdf",
                    "document_id": "object-a",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "project",
                    "project_ids": ["p1"],
                },
                {
                    "name": "同名论文.pdf",
                    "document_id": "object-b",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "project",
                    "project_ids": ["p1"],
                },
            ]
        },
    )
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-name-only-token")

    response = TestClient(mcp_server.app).post(
        "/multimodal/workflows/plan",
        headers={"X-SoulDrive-Token": "workflow-name-only-token"},
        json={
            "workflow_id": "paper_review",
            "project_id": "p1",
            "document_names": ["同名论文.pdf"],
        },
    )

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert any("同名文档不唯一" in item for item in response.json()["missing_inputs"])


def test_workflow_plan_rejects_document_id_name_mismatch(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: SimpleNamespace(root_path="test-workspace"))
    monkeypatch.setattr(mcp_server, "_project_exists", lambda *_args: True)
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_document_library_payload",
        lambda _workspace: {
            "documents": [
                {
                    "name": "实际文档.pdf",
                    "document_id": "object-a",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "project",
                    "project_ids": ["p1"],
                },
            ]
        },
    )
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-document-label-token")

    response = TestClient(mcp_server.app).post(
        "/multimodal/workflows/plan",
        headers={"X-SoulDrive-Token": "workflow-document-label-token"},
        json={
            "workflow_id": "paper_review",
            "project_id": "p1",
            "document_names": ["伪造标签.pdf"],
            "document_ids": ["object-a"],
        },
    )

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert any("ID 与名称不匹配" in item for item in response.json()["missing_inputs"])


def test_workflow_plan_api_blocks_an_incompatible_explicit_run_pair(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: object())
    monkeypatch.setattr(mcp_server, "_project_exists", lambda *_args: True)
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_load_explicit_experiment_comparison",
        lambda *_args: {
            "status": "not_comparable",
            "reason": "数据集版本不一致",
        },
    )
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-plan-token")

    response = TestClient(mcp_server.app).post(
        "/multimodal/workflows/plan",
        headers={"X-SoulDrive-Token": "workflow-plan-token"},
        json={
            "workflow_id": "experiment_compare",
            "project_id": "p1",
            "baseline_run_id": "run-baseline",
            "current_run_id": "run-current",
        },
    )

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert "数据集版本不一致" in response.json()["next_action"]


def test_project_playbook_approval_endpoint_is_version_and_project_scoped(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        catalog = ProjectCatalog(workspace)
        try:
            catalog.create_project("论文项目", project_id="p1")
        finally:
            catalog.close()

        monkeypatch.setattr(mcp_server, "require_workspace_mounted", lambda: None)
        monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
        monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
        monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
        monkeypatch.setenv("SOULDRIVE_API_TOKEN", "playbook-token")
        client = TestClient(mcp_server.app)
        headers = {"X-SoulDrive-Token": "playbook-token"}

        approved = client.post(
            "/projects/p1/playbooks/paper_review/approval",
            headers=headers,
            json={"playbook_version": "1.0", "approved": True},
        )
        listed = client.get(
            "/multimodal/workflows",
            params={"project_id": "p1"},
            headers=headers,
        )
        stale = client.post(
            "/projects/p1/playbooks/paper_review/approval",
            headers=headers,
            json={"playbook_version": "0.9", "approved": True},
        )

    assert approved.status_code == 200
    assert approved.json()["approval"]["status"] == "approved"
    paper_review = next(item for item in listed.json()["workflows"] if item["workflow_id"] == "paper_review")
    assert paper_review["approval"]["status"] == "approved"
    assert stale.status_code == 409


def test_chat_receives_a_short_workflow_contract_for_rag(monkeypatch):
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    class FakeRag:
        query = ""
        context = None

        def generate_response_stream(self, query, **kwargs):
            self.query = query
            self.context = kwargs["interaction_context"]
            yield "ok"

    rag = FakeRag()
    state = {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}}
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "get_runtime_state", lambda: state)
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: object())
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", rag))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-chat-token")

    client = TestClient(mcp_server.app)
    response = client.post(
        "/chat",
        headers={"X-SoulDrive-Token": "workflow-chat-token"},
        json={
            "query": "整理这篇论文",
            "workflow_id": "paper_review",
            "active_context": {
                "project_id": "p1",
                "document_names": ["论文A.pdf"],
            },
        },
    )

    assert response.status_code == 200
    assert response.text.endswith("ok")
    assert rag.query == "整理这篇论文"
    assert rag.context["workflow_id"] == "paper_review"


def test_chat_blocks_text_only_experiment_compare_before_retrieval(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    class FakeRag:
        def generate_response_stream(self, *_args, **_kwargs):
            raise AssertionError("workflow input gate must run before RAG")
            yield "unreachable"

    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: object())
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", FakeRag()))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-input-gate-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "workflow-input-gate-token"},
        json={
            "query": "比较当前实验",
            "workflow_id": "experiment_compare",
            "active_context": {"project_id": "p1"},
        },
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["workflow_plan"]["ready"] is False
    assert "至少选择两份实验材料" in payload["workflow_plan"]["missing_inputs"][0]


def test_chat_rejects_client_declared_unindexed_workflow_document(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    called = {"value": False}

    class FakeRag:
        def generate_response_stream(self, *_args, **_kwargs):
            called["value"] = True
            yield "ok"

    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: SimpleNamespace(root_path="test-workspace"))
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(
        mcp_server,
        "_document_library_payload",
        lambda _workspace: {
            "documents": [
                {
                    "name": "论文A.pdf",
                    "indexed": False,
                    "index_status": "pending",
                    "scope": "project",
                    "project_ids": ["p1"],
                }
            ]
        },
    )
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", FakeRag()))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-document-validation-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "workflow-document-validation-token"},
        json={
            "query": "整理这篇论文",
            "workflow_id": "paper_review",
            "active_context": {
                "project_id": "p1",
                "document_names": ["论文A.pdf"],
            },
        },
    )

    assert response.status_code == 422
    assert called["value"] is False
    assert "尚未完成索引" in response.json()["workflow_plan"]["missing_inputs"][0]


def test_chat_rejects_explicit_document_id_label_mismatch_before_rag(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    called = {"value": False}

    class FakeRag:
        def generate_response_stream(self, *_args, **_kwargs):
            called["value"] = True
            yield "不应进入 RAG"

    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: SimpleNamespace(root_path="test-workspace"))
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(
        mcp_server,
        "_document_library_payload",
        lambda _workspace: {
            "documents": [
                {
                    "name": "实际文档.pdf",
                    "document_id": "object-a",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "project",
                    "project_ids": ["p1"],
                },
            ]
        },
    )
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", FakeRag()))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "chat-document-label-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "chat-document-label-token"},
        json={
            "query": "这份材料的结论是什么？",
            "active_context": {
                "project_id": "p1",
                "document_names": ["伪造标签.pdf"],
                "document_ids": ["object-a"],
            },
        },
    )

    assert response.status_code == 409
    assert called["value"] is False
    assert "文档 ID 与名称不匹配" in response.json()["document_context"]["issues"][0]


def test_chat_rejects_name_only_duplicate_documents_before_rag(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    called = {"value": False}

    class FakeRag:
        def generate_response_stream(self, *_args, **_kwargs):
            called["value"] = True
            yield "不应进入 RAG"

    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: SimpleNamespace(root_path="test-workspace"))
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(
        mcp_server,
        "_document_library_payload",
        lambda _workspace: {
            "documents": [
                {
                    "name": "同名论文.pdf",
                    "document_id": "object-a",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "project",
                    "project_ids": ["p1"],
                },
                {
                    "name": "同名论文.pdf",
                    "document_id": "object-b",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "project",
                    "project_ids": ["p1"],
                },
            ]
        },
    )
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", FakeRag()))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "chat-duplicate-document-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "chat-duplicate-document-token"},
        json={
            "query": "这份材料的结论是什么？",
            "active_context": {
                "project_id": "p1",
                "document_names": ["同名论文.pdf"],
            },
        },
    )

    assert response.status_code == 409
    assert called["value"] is False
    assert "同名文档不唯一" in response.json()["document_context"]["issues"][0]


def test_chat_rejects_project_document_selection_without_project_scope_before_rag(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    called = {"value": False}

    class FakeRag:
        def generate_response_stream(self, *_args, **_kwargs):
            called["value"] = True
            yield "不应进入 RAG"

    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: SimpleNamespace(root_path="test-workspace"))
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: None)
    monkeypatch.setattr(
        mcp_server,
        "_document_library_payload",
        lambda _workspace: {
            "documents": [
                {
                    "name": "项目论文.pdf",
                    "document_id": "object-project-a",
                    "indexed": True,
                    "index_status": "indexed",
                    "scope": "project",
                    "project_ids": ["p1"],
                },
            ]
        },
    )
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", FakeRag()))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "chat-project-document-scope-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "chat-project-document-scope-token"},
        json={
            "query": "请在全局资料中核对这份论文。",
            "active_context": {
                "document_ids": ["object-project-a"],
                "document_names": ["项目论文.pdf"],
            },
        },
    )

    assert response.status_code == 409
    assert called["value"] is False
    assert "不属于当前项目" in response.json()["document_context"]["issues"][0]


def test_chat_resolves_project_from_session_when_active_context_omits_project(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    class SessionStore:
        def session_metadata(self, session_id):
            assert session_id == "session-p1"
            return {"session_id": session_id, "project_id": "p1", "workflow_id": "paper_review"}

        def load_context(self, _session_id, limit=8):
            assert limit == 8
            return {"history": [], "summary": None}

        def append_message(self, *_args, **_kwargs):
            return None

        def compact_session(self, *_args, **_kwargs):
            return None

    class FakeVlmRuntime:
        def release_vlm(self):
            return None

    class FakeRag:
        context = None

        def generate_response_stream(self, query, **kwargs):
            self.context = kwargs["interaction_context"]
            # The user's words are the retrieval/generation query.  The
            # session role is structured context, so its output contract
            # must not be concatenated into the retrieval query.
            assert query == "RAG 的核心思想是什么？"
            yield "RAG 回答 [E1]"

    rag = FakeRag()
    workspace = object()
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
    monkeypatch.setattr(mcp_server, "_conversation_store", lambda _workspace: SessionStore())
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda _workspace, project_id: "research" if project_id == "p1" else None)
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(mcp_server, "get_topic5_runtime", lambda: FakeVlmRuntime())
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", rag))
    monkeypatch.setattr(mcp_server, "_learning_context_for_query", lambda *args, **kwargs: {
        "memories": [], "skills": [], "memory_capsule": {"items": [], "text": ""},
        "memory_gate": {"decision": "skip", "accepted_count": 0, "rejected_count": 0},
        "skill_matches": [], "skill_execution": None,
    })
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "session-rag-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "session-rag-token"},
        json={
            "query": "RAG 的核心思想是什么？",
            "session_id": "session-p1",
            "active_context": {"document_names": ["论文A.pdf"]},
        },
    )

    assert response.status_code == 200
    assert response.text.endswith("RAG 回答 [E1]")
    assert rag.context["active_context"]["project_id"] == "p1"
    assert rag.context["workflow_id"] == "paper_review"


def test_chat_rechecks_session_scope_when_indexing_state_changes_before_execution(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    class SessionStore:
        def session_metadata(self, session_id):
            assert session_id == "bound-session"
            return {"session_id": session_id, "project_id": "project-a", "workflow_id": "paper_review"}

        def load_context(self, _session_id, limit=8):
            assert limit == 8
            return {"history": [], "summary": None}

    class FakeVlmRuntime:
        def release_vlm(self):
            return None

    class FakeRag:
        called = False

        def generate_response_stream(self, _query, **_kwargs):
            self.called = True
            yield "越界执行"

    runtime_states = iter([
        {"locked": False, "software_unlocked": True, "indexing": {"status": "indexing"}},
        {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    ])
    rag = FakeRag()
    workspace = object()
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "get_runtime_state", lambda: next(runtime_states))
    monkeypatch.setattr(mcp_server, "indexer_process", None)
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
    monkeypatch.setattr(mcp_server, "_conversation_store", lambda _workspace: SessionStore())
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "get_topic5_runtime", lambda: FakeVlmRuntime())
    monkeypatch.setattr(mcp_server, "_learning_context_for_query", lambda *args, **kwargs: {
        "memories": [], "skills": [], "memory_capsule": {"items": [], "text": ""},
        "memory_gate": {"decision": "skip", "accepted_count": 0, "rejected_count": 0},
        "skill_matches": [], "skill_execution": None,
    })
    monkeypatch.setattr(mcp_server, "_retrieval_feedback_for_query", lambda *args, **kwargs: [])
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", rag))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "index-race-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "index-race-token"},
        json={
            "query": "不能使用客户端项目覆盖持久会话",
            "session_id": "bound-session",
            "active_context": {"project_id": "project-b"},
        },
    )

    assert response.status_code == 409
    assert "another project" in response.json()["error"]
    assert rag.called is False


def test_chat_rejects_request_workflow_that_conflicts_with_session_role(monkeypatch):
    class SessionStore:
        def session_metadata(self, session_id):
            assert session_id == "role-bound-session"
            return {
                "session_id": session_id,
                "project_id": "p1",
                "workflow_id": "paper_review",
            }

    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: object())
    monkeypatch.setattr(mcp_server, "_conversation_store", lambda _workspace: SessionStore())
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-conflict-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "workflow-conflict-token"},
        json={
            "query": "比较实验",
            "session_id": "role-bound-session",
            "workflow_id": "experiment_compare",
            "active_context": {"project_id": "p1"},
        },
    )

    assert response.status_code == 409
    assert "paper_review" in response.json()["error"]


def test_chat_rejects_workflow_override_on_generic_session(monkeypatch):
    class SessionStore:
        def session_metadata(self, session_id):
            assert session_id == "generic-session"
            return {
                "session_id": session_id,
                "project_id": "p1",
                "workflow_id": None,
            }

    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: object())
    monkeypatch.setattr(mcp_server, "_conversation_store", lambda _workspace: SessionStore())
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "generic-workflow-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "generic-workflow-token"},
        json={
            "query": "总结这篇论文",
            "session_id": "generic-session",
            "workflow_id": "paper_review",
            "active_context": {"project_id": "p1"},
        },
    )

    assert response.status_code == 409
    assert "generic workflow" in response.json()["error"]
    assert "paper_review" in response.json()["error"]


def test_session_role_rejects_experiment_selection_outside_experiment_compare(monkeypatch):
    class SessionStore:
        def session_metadata(self, session_id):
            assert session_id == "paper-role-session"
            return {
                "session_id": session_id,
                "project_id": "p1",
                "workflow_id": "paper_review",
            }

    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: object())
    monkeypatch.setattr(mcp_server, "_conversation_store", lambda _workspace: SessionStore())
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-selection-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "workflow-selection-token"},
        json={
            "query": "总结当前论文",
            "session_id": "paper-role-session",
            "active_context": {
                "project_id": "p1",
                "baseline_run_id": "baseline",
                "current_run_id": "current",
            },
        },
    )

    assert response.status_code == 409
    assert "experiment_compare" in response.json()["error"]


def test_chat_workflow_treats_structured_visual_events_as_image_evidence(monkeypatch):
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    class FakeRag:
        query = ""
        context = None

        def generate_response_stream(self, query, **kwargs):
            self.query = query
            self.context = kwargs["interaction_context"]
            yield "ok"

    rag = FakeRag()
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "get_runtime_state", lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}})
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: object())
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", rag))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-visual-event-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "workflow-visual-event-token"},
        json={
            "query": "比较这张图里的两组实验数据",
            "workflow_id": "experiment_compare",
            "active_context": {"project_id": "p1"},
            "perception_events": [{
                "event_id": "ocr-region-1",
                "modality": "ocr_region",
                "timestamp_ms": 1000,
                "text": "基线 0.62；当前 0.79",
                "confidence": 0.92,
                "bbox": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.4},
            }],
        },
    )

    assert response.status_code == 200
    assert response.text.endswith("ok")
    assert rag.query == "比较这张图里的两组实验数据"
    assert rag.context["workflow_id"] == "experiment_compare"


def test_chat_bridges_explicit_comparable_runs_as_m_evidence(tmp_path, monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    workspace = SoulDriveWorkspace.from_drive(str(tmp_path)).ensure()
    catalog = ProjectCatalog(workspace)
    try:
        catalog.create_project("实验项目", project_id="p1")
        experiment_id = catalog.create_experiment("p1", "alignment")
        baseline = catalog.create_experiment_run(
            experiment_id,
            "2026-07-12T10:00:00+08:00",
            run_id="run-baseline",
            dataset_version="dataset-v1",
            config_ref="config-a",
            evaluation_script_ref="eval-v1",
        )
        current = catalog.create_experiment_run(
            experiment_id,
            "2026-08-12T10:00:00+08:00",
            run_id="run-current",
            dataset_version="dataset-v1",
            config_ref="config-b",
            evaluation_script_ref="eval-v1",
        )
        metric_id = catalog.create_metric_definition("accuracy", "%", "higher", "metric-v1")
        catalog.record_measurement(baseline, metric_id, 0.86, "test", source_locator="row:2")
        catalog.record_measurement(current, metric_id, 0.81, "test", source_locator="row:2")
    finally:
        catalog.close()

    class FakeRag:
        context = None

        def generate_response_stream(self, query, **kwargs):
            self.context = kwargs.get("interaction_context")
            yield "ok"

    rag = FakeRag()
    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: workspace)
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", rag))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "experiment-chat-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "experiment-chat-token"},
        json={
            "query": "为什么当前实验结果比上个月差？",
            "workflow_id": "experiment_compare",
            "active_context": {
                "project_id": "p1",
                "baseline_run_id": "run-baseline",
                "current_run_id": "run-current",
            },
        },
    )

    assert response.status_code == 200
    assert response.text.endswith("ok")
    comparison = rag.context["experiment_comparison"]
    assert comparison["status"] == "comparable"
    assert comparison["changes"][0]["absolute_delta"] == -0.05


def test_chat_blocks_plain_text_before_workflow_inputs_are_complete(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    class FakeRag:
        def generate_response_stream(self, *_args, **_kwargs):
            yield "ok"

    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(mcp_server, "get_runtime_state", lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}})
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: object())
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(
        mcp_server,
        "_playbook_approval_for_project",
        lambda *_args: {"status": "approved", "playbook_version": "1.0"},
    )
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", FakeRag()))
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-block-token")

    client = TestClient(mcp_server.app)
    response = client.post(
        "/chat",
        headers={"X-SoulDrive-Token": "workflow-block-token"},
        json={
            "query": "整理这篇论文",
            "workflow_id": "paper_review",
            "active_context": {"project_id": "p1"},
        },
    )

    assert response.status_code == 422
    assert response.json()["workflow_plan"]["ready"] is False
    assert "至少选择一篇已索引论文" in response.json()["workflow_plan"]["missing_inputs"]


def test_chat_blocks_plain_text_before_playbook_review(monkeypatch):
    class Audit:
        def append_event(self, *_args, **_kwargs):
            return None

    class FakeRag:
        def generate_response_stream(self, *_args, **_kwargs):
            yield "ok"

    monkeypatch.setattr(mcp_server, "require_software_unlock", lambda: None)
    monkeypatch.setattr(
        mcp_server,
        "get_runtime_state",
        lambda: {"locked": False, "software_unlocked": True, "indexing": {"status": "idle"}},
    )
    monkeypatch.setattr(mcp_server, "current_audit_logger", lambda: Audit())
    monkeypatch.setattr(mcp_server, "current_workspace", lambda: object())
    monkeypatch.setattr(mcp_server, "_project_mode_for_id", lambda *_args: "research")
    monkeypatch.setattr(mcp_server, "_playbook_approval_for_project", lambda *_args: None)
    monkeypatch.setattr(mcp_server, "ensure_runtime_loaded", lambda: setattr(mcp_server, "rag", FakeRag()))
    monkeypatch.setattr(mcp_server, "rate_limiter", SlidingWindowRateLimiter(limit=1000))
    monkeypatch.setenv("SOULDRIVE_API_TOKEN", "workflow-review-block-token")

    response = TestClient(mcp_server.app).post(
        "/chat",
        headers={"X-SoulDrive-Token": "workflow-review-block-token"},
        json={
            "query": "整理这篇论文",
            "workflow_id": "paper_review",
            "active_context": {
                "project_id": "p1",
                "document_names": ["论文A.pdf"],
            },
        },
    )

    assert response.status_code == 422
    payload = response.json()
    assert payload["workflow_plan"]["ready"] is False
    assert "请先审核通过当前项目的 Playbook" in payload["workflow_plan"]["missing_inputs"]
