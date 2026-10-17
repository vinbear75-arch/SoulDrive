import hashlib
import inspect
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from core.audit_log import append_audit_event_best_effort
from core.experiment_ledger import build_experiment_evidence, compare_experiment_runs
from core.paths import PROJECT_ROOT, app_root
from core.openxml_presentation import render_presentation
from core.project_catalog import ProjectCatalog
from core.secure_vector_store import SecureVectorStore
from core.task_journal import TaskJournal, TaskPublicError, process_owner_id


PRESENTATION_PLAN_STEPS = (
    "collect_evidence",
    "build_outline",
)
PRESENTATION_GENERATION_STEPS = (
    "load_confirmed_plan",
    "render_pptx",
    "verify_pptx",
    "open_in_wps",
)
# Kept as an import-compatible name for callers that only inspect the old
# constant; new tasks use one of the two phase-specific tuples above.
PRESENTATION_STEPS = PRESENTATION_GENERATION_STEPS
ALLOWED_AUDIENCES = {"research", "technical", "government"}
SENTENCE_PATTERN = re.compile(r"[^。！？!?；;\n]{12,220}[。！？!?；;]?")
NOISE_PATTERN = re.compile(
    r"^(?:目录|目\s*录|contents|copyright|版权所有|第\s*\d+\s*页|page\s+\d+)$",
    flags=re.IGNORECASE,
)
TERM_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,6}")
STOP_TERMS = {
    "以及", "通过", "进行", "相关", "主要", "可以", "需要", "包括", "其中", "对于",
    "本项目", "本方案", "文件", "项目", "工作", "建设", "实施", "要求", "内容", "管理",
    "the", "and", "for", "with", "from", "this", "that",
}


class PresentationService:
    def __init__(
        self,
        workspace,
        keys,
        *,
        audit_logger=None,
        outline_generator: Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]] | None = None,
        style_skill_lookup: Callable[..., dict[str, Any] | None] | None = None,
    ):
        self.workspace = workspace
        self.keys = keys
        self.audit_logger = audit_logger
        self.outline_generator = outline_generator
        self.style_skill_lookup = style_skill_lookup

    def create_plan(self, request: dict[str, Any]) -> dict[str, Any]:
        inputs = validate_presentation_request(request)
        comparison = self._load_experiment_comparison(inputs)
        if comparison is not None:
            inputs["experiment_comparison"] = comparison
        if inputs.get("style_skill_id"):
            style = None
            if self.style_skill_lookup:
                lookup_parameters = inspect.signature(self.style_skill_lookup).parameters.values()
                accepts_project = any(
                    parameter.kind == inspect.Parameter.VAR_POSITIONAL
                    or parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in lookup_parameters
                ) or len(inspect.signature(self.style_skill_lookup).parameters) >= 2
                style = (
                    self.style_skill_lookup(inputs["style_skill_id"], inputs.get("project_id"))
                    if accepts_project
                    else self.style_skill_lookup(inputs["style_skill_id"])
                )
            if style is None:
                raise ValueError("PPT 风格 Skill 不存在、未启用或不是模板类型")
            inputs["style_skill"] = style
        inputs["request_digest"] = _presentation_request_digest(inputs)
        task_id = _stable_presentation_task_id("presentation.plan", inputs["request_id"])
        inputs["task_id"] = task_id
        journal = TaskJournal(self.workspace, self.keys)
        try:
            try:
                existing = journal.get_task(task_id)
            except KeyError:
                existing = None
            if existing is not None:
                if existing["inputs"].get("request_digest") != inputs["request_digest"]:
                    raise ValueError("request_id 已用于其他 PPT 计划，请更换 request_id")
                replay = dict(existing)
                replay["replayed"] = True
                return replay
            artifact_id = self._create_presentation_artifact(inputs)
            if artifact_id:
                inputs["artifact_id"] = artifact_id
            task = journal.create_task(
                "presentation.plan",
                inputs,
                PRESENTATION_PLAN_STEPS,
                task_id=task_id,
                idempotency_key=f"presentation.plan:{inputs['request_id']}",
                repetition_inputs=_presentation_repetition_inputs(inputs),
            )
            self._audit("presentation.plan_created", task, {"source_count": len(inputs["source_filenames"])})
            return task
        except Exception:
            artifact_id = locals().get("artifact_id")
            if artifact_id:
                self._record_artifact_failure(str(artifact_id), "task_creation_failed")
            raise
        finally:
            journal.close()

    def _load_experiment_comparison(self, inputs: dict[str, Any]) -> dict[str, Any] | None:
        if not inputs.get("baseline_run_id"):
            return None
        catalog = ProjectCatalog(self.workspace)
        try:
            comparison = compare_experiment_runs(catalog, inputs["baseline_run_id"], inputs["current_run_id"],
                                                 project_id=inputs["project_id"])
        finally:
            catalog.close()
        if comparison.get("status") != "comparable":
            raise ValueError("所选实验运行不可比，请先核对项目、指标与数据版本")
        if inputs.get("experiment_comparison") is not None and comparison != inputs["experiment_comparison"]:
            raise ValueError("实验账本证据已变化，请重新生成并确认大纲")
        return comparison

    def create_task(self, request: dict[str, Any]) -> dict[str, Any]:
        """Compatibility entry point; generation now always starts with a plan."""
        return self.create_plan(request)

    def run_task(self, task_id: str, *, suppress_wps: bool = False) -> dict[str, Any]:
        started_at = time.monotonic()
        journal = TaskJournal(self.workspace, self.keys)
        try:
            journal.recover_orphaned_tasks()
            task = journal.get_task(task_id)
            if task["status"] == "completed":
                # A durable task can commit before its catalog projection (or
                # audit sink) is available.  Replaying a terminal task must
                # therefore repair the local artifact projection without
                # re-running any side-effecting step.
                self._sync_presentation_artifact(task.get("inputs") or {}, task)
                return task
            task_inputs = task["inputs"]
            if task["kind"] == "presentation.plan":
                handlers = {
                    "collect_evidence": self._collect_evidence_step,
                    "build_outline": self._build_outline_step,
                }
                result_builder = lambda outputs: self._build_plan_result(
                    task_inputs,
                    outputs,
                    round((time.monotonic() - started_at) * 1000, 1),
                )
            elif task["kind"] == "presentation.generate":
                if not task_inputs.get("plan_task_id"):
                    raise ValueError("未确认的旧 PPT 任务不能继续，请先生成并确认新的大纲")
                suppress_wps = suppress_wps or task["status"] in {"interrupted", "failed"}
                handlers = {
                    "load_confirmed_plan": self._load_confirmed_plan_step,
                    "render_pptx": self._render_pptx_step,
                    "verify_pptx": self._verify_pptx_step,
                    "open_in_wps": (
                        (lambda inputs, outputs, step_context: self._open_in_wps_step(
                            {**inputs, "open_in_wps": False}, outputs, step_context
                        ))
                        if suppress_wps else self._open_in_wps_step
                    ),
                }
                result_builder = lambda outputs: self._build_presentation_result(
                    task_inputs,
                    outputs,
                    round((time.monotonic() - started_at) * 1000, 1),
                )
            else:
                raise ValueError(f"unsupported presentation task kind: {task['kind']}")
            result = journal.run_steps(
                task_id,
                process_owner_id(uuid.uuid4().hex[:8]),
                handlers,
                result_builder,
                include_step_context=True,
            )
            self._sync_presentation_artifact(task_inputs, result)
            event = (
                "presentation.task_completed"
                if result["status"] == "completed"
                else "presentation.task_cancelled"
                if result["status"] == "cancelled"
                else "presentation.task_failed"
            )
            self._audit(event, result, {"error": result.get("error")})
            return result
        finally:
            journal.close()

    def resume_task(self, task_id: str) -> dict[str, Any]:
        journal = TaskJournal(self.workspace, self.keys)
        try:
            task = journal.get_task(task_id)
        finally:
            journal.close()
        if task["status"] == "completed":
            return task
        if task["status"] == "cancelled":
            raise ValueError("cancelled task cannot be resumed")
        return self.run_task(task_id, suppress_wps=True)

    def get_task(self, task_id: str) -> dict[str, Any]:
        journal = TaskJournal(self.workspace, self.keys)
        try:
            journal.recover_orphaned_tasks()
            return journal.public_task(task_id)
        finally:
            journal.close()

    def list_tasks(
        self,
        limit: int = 30,
        *,
        project_id: str | None = None,
        global_only: bool = False,
    ) -> list[dict[str, Any]]:
        journal = TaskJournal(self.workspace, self.keys)
        try:
            journal.recover_orphaned_tasks()
            safe_limit = max(1, min(int(limit), 100))
            scoped_tasks = []
            for kind in ("presentation.plan", "presentation.generate"):
                scoped_tasks.extend(
                    journal.list_tasks(
                        kind=kind,
                        limit=safe_limit,
                        project_id=project_id,
                        global_only=global_only,
                    )
                )
            scoped_tasks.sort(
                key=lambda task: (
                    -float(task.get("updated_at") or 0.0),
                    str(task.get("task_id") or ""),
                )
            )
            return scoped_tasks[:safe_limit]
        finally:
            journal.close()

    def request_cancel(self, task_id: str) -> dict[str, Any]:
        journal = TaskJournal(self.workspace, self.keys)
        try:
            task = journal.request_cancel(task_id)
        finally:
            journal.close()
        self._sync_presentation_artifact(task.get("inputs") or {}, task)
        self._audit("presentation.task_cancel_requested", task, {"error": task.get("error")})
        return self.get_task(task_id)

    def confirm_plan(self, plan_task_id: str, plan_digest: str) -> dict[str, Any]:
        journal = TaskJournal(self.workspace, self.keys)
        try:
            plan_task = journal.get_task(plan_task_id)
            if plan_task["kind"] != "presentation.plan":
                raise ValueError("只能确认 PPT 大纲任务")
            if plan_task["status"] != "completed":
                raise ValueError("PPT 大纲尚未完成，不能确认")
            result = dict(plan_task.get("result") or {})
            if str(result.get("plan_digest") or "") != str(plan_digest or ""):
                raise ValueError("PPT 大纲已变化，请重新查看后确认")
            inputs = dict(plan_task["inputs"])
            self._require_experiment_outline(inputs, result)
            _require_generated_outline_evidence(inputs, result)
            generation_task_id = _stable_presentation_task_id(
                "presentation.generate",
                f"{plan_task_id}:{plan_digest}",
            )
            existing = None
            try:
                existing = journal.get_task(generation_task_id)
            except KeyError:
                pass
            if existing is None:
                self._load_experiment_comparison(inputs)
                snapshot = dict(result.get("evidence_snapshot") or {})
                current_records = collect_indexed_evidence(
                    self.workspace,
                    self.keys,
                    list(inputs.get("source_filenames") or []),
                    project_id=inputs.get("project_id"),
                    source_version_ids=inputs.get("source_version_ids"),
                    source_document_ids=inputs.get("source_document_ids"),
                )
                current_fingerprints = _source_fingerprints(
                    current_records,
                    list(inputs.get("source_filenames") or []),
                    list(inputs.get("source_document_ids") or []),
                )
                if current_fingerprints != snapshot.get("source_fingerprints"):
                    raise ValueError("所选证据已变化，请重新生成大纲")
                generation_inputs = {
                    key: inputs.get(key)
                    for key in (
                        "source_filenames", "source_document_ids", "source_version_ids", "title", "audience",
                        "max_slides", "open_in_wps", "style_skill_id", "style_skill",
                        "project_id",
                        "baseline_run_id", "current_run_id", "experiment_comparison",
                    )
                }
                generation_inputs.update({
                    "task_id": generation_task_id,
                    "generation_task_id": generation_task_id,
                    "plan_task_id": plan_task_id,
                    "request_id": inputs.get("request_id"),
                    "plan_digest": plan_digest,
                    "artifact_id": inputs.get("artifact_id"),
                })
                generation_task = journal.create_task(
                    "presentation.generate",
                    generation_inputs,
                    PRESENTATION_GENERATION_STEPS,
                    task_id=generation_task_id,
                    idempotency_key=f"presentation.generate:{plan_task_id}:{plan_digest}",
                    repetition_inputs=_presentation_repetition_inputs(
                        inputs,
                        plan_digest=plan_digest,
                    ),
                )
            else:
                # The journal may have committed immediately before the
                # project catalog update.  Reuse the task, but still repair
                # the confirmation marker after a process restart.
                generation_task = existing
        except sqlite3.IntegrityError:
            generation_task = self.get_task(generation_task_id)
        finally:
            journal.close()

        artifact_id = str(inputs.get("artifact_id") or "").strip()
        if artifact_id:
            catalog = ProjectCatalog(self.workspace)
            try:
                artifact = catalog.get_artifact(artifact_id)
                if artifact is not None and artifact.get("confirmation_status") == "draft":
                    catalog.set_artifact_status(artifact_id, "confirmed")
                    catalog.update_artifact_provenance(
                        artifact_id,
                        confirmation_record_patch={
                            "status": "confirmed",
                            "mode": "explicit_outline_digest_confirmation",
                            "plan_task_id": plan_task_id,
                            "plan_digest": plan_digest,
                            "generation_task_id": generation_task_id,
                            "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        },
                        change_summary="用户已确认实际 PPT 大纲摘要；生成任务获得执行资格。",
                    )
                elif artifact is not None and artifact.get("confirmation_status") not in {"confirmed"}:
                    raise ValueError("PPT 大纲已被取消，不能恢复生成")
            finally:
                catalog.close()
        self._audit("presentation.plan_confirmed", generation_task, {"plan_task_id": plan_task_id})
        return self.get_task(generation_task_id)

    def _create_presentation_artifact(self, inputs: dict[str, Any]) -> str | None:
        project_id = str(inputs.get("project_id") or "").strip()
        if not project_id:
            return None
        catalog = ProjectCatalog(self.workspace)
        try:
            if catalog.conn.execute(
                "SELECT 1 FROM projects WHERE project_id = ?",
                (project_id,),
            ).fetchone() is None:
                raise ValueError("unknown project")
            plan = {
                "task_id": inputs["task_id"],
                "request_id": inputs["request_id"],
                "request_digest": inputs["request_digest"],
                "source_filenames": list(inputs["source_filenames"]),
                "source_document_ids": list(inputs.get("source_document_ids") or []),
                "source_version_ids": list(inputs.get("source_version_ids") or []),
                "title": inputs.get("title") or "",
                "audience": inputs["audience"],
                "max_slides": inputs["max_slides"],
                "open_in_wps": bool(inputs.get("open_in_wps")),
                "style_skill_id": inputs.get("style_skill_id") or None,
                "side_effect": "local_pptx_and_optional_wps_open",
                "requires_user_confirmation": True,
                "confirmation_status": "draft",
            }
            confirmation_record = {
                "status": "draft",
                "mode": "outline_review_required",
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "wps_open": "requested" if inputs.get("open_in_wps") else "not_requested",
                "generation_status": "not_started",
            }
            artifact_id = catalog.create_artifact(
                project_id,
                "presentation_plan",
                plan,
                idempotency_key=f"presentation.plan:{inputs['request_id']}",
                source_version_ids=list(inputs.get("source_version_ids") or []),
                confirmation_status="draft",
                confirmation_record=confirmation_record,
                change_summary="已创建 PPT 大纲计划；实际生成前必须查看并确认大纲摘要。",
            )
            artifact = catalog.get_artifact(artifact_id)
            if artifact and artifact.get("plan", {}).get("request_digest") != inputs["request_digest"]:
                raise ValueError("request_id 已用于其他 PPT 计划，请更换 request_id")
            return artifact_id
        finally:
            catalog.close()

    def _record_artifact_failure(self, artifact_id: str, reason: str) -> None:
        catalog = ProjectCatalog(self.workspace)
        try:
            catalog.update_artifact_provenance(
                artifact_id,
                confirmation_record_patch={
                    "generation_status": "failed",
                    "failure_reason": reason,
                    "status_changed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                change_summary="生成任务未创建成功；已记录失败状态，未把失败结果标为可交付。",
            )
        finally:
            catalog.close()

    @staticmethod
    def _build_plan_result(
        inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        elapsed_ms: float,
    ) -> dict[str, Any]:
        records = outputs["collect_evidence"]["records"]
        outline = dict(outputs["build_outline"])
        version_ids, source_refs = _presentation_source_refs(inputs, records)
        return {
            "phase": "outline_review",
            "request_id": inputs.get("request_id"),
            "plan_task_id": inputs.get("task_id"),
            "plan_digest": _presentation_plan_digest(outline),
            "outline": outline,
            "outline_source": outline.get("outline_source"),
            "evidence_validation": outline.get("evidence_validation") or {},
            "slide_count": len(outline.get("slides") or []),
            "source_count": outputs["collect_evidence"]["source_count"],
            "source_document_ids": list(inputs.get("source_document_ids") or []),
            "source_version_ids": version_ids,
            "source_refs": source_refs,
            "source_fingerprints": _source_fingerprints(
                records,
                inputs["source_filenames"],
                list(inputs.get("source_document_ids") or []),
            ),
            "artifact_id": inputs.get("artifact_id"),
            "evidence_snapshot": {
                "records": records,
                "source_fingerprints": _source_fingerprints(
                    records,
                    inputs["source_filenames"],
                    list(inputs.get("source_document_ids") or []),
                ),
                "source_version_ids": version_ids,
            },
            **({"experiment_comparison": inputs["experiment_comparison"],
                "measurement_evidence": build_experiment_evidence(inputs["experiment_comparison"])}
               if inputs.get("experiment_comparison") else {}),
            "skill_application": outline.get("skill_application") or {},
            "evidence_metrics": presentation_evidence_metrics(
                outline,
                outputs["collect_evidence"]["source_count"],
                records,
                list(inputs.get("source_document_ids") or []),
            ),
            "elapsed_ms": elapsed_ms,
        }

    @staticmethod
    def _build_presentation_result(
        inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        elapsed_ms: float,
    ) -> dict[str, Any]:
        confirmed = outputs["load_confirmed_plan"]
        records = confirmed["records"]
        outline = confirmed["plan"]
        version_ids = sorted({
            str(record.get("asset_version_id"))
            for record in records
            if str(record.get("asset_version_id") or "").strip()
        })
        source_refs = []
        _, source_refs = _presentation_source_refs(inputs, records)
        verify_output = outputs["verify_pptx"]
        skill_execution = verify_output.get("skill_execution")
        skill_application = dict(outline.get("skill_application") or {
            "skill_id": (inputs.get("style_skill") or {}).get("skill_id"),
            "skill_title": (inputs.get("style_skill") or {}).get("title"),
            "mode": "bound_for_outline_and_render",
            "instructions_applied": bool(inputs.get("style_skill")),
        })
        if skill_execution is not None:
            skill_application["script_execution"] = skill_execution
        return {
            "output_path": outputs["render_pptx"]["output_path"],
            "slide_count": outputs["verify_pptx"]["slide_count"],
            "renderer": outputs["render_pptx"]["renderer"],
            "opened_in_wps": outputs.get("open_in_wps", {}).get("opened", False),
            "source_count": confirmed["source_count"],
            "source_document_ids": list(inputs.get("source_document_ids") or []),
            "source_version_ids": version_ids,
            "source_refs": source_refs,
            "artifact_id": inputs.get("artifact_id"),
            "outline_source": outline.get("outline_source") or inputs.get("outline_source") or "extractive",
            "evidence_validation": outline.get("evidence_validation") or {},
            "skill_application": skill_application,
            "skill_execution": skill_execution,
            "skill_gate_passed": verify_output.get("skill_gate_passed"),
            "evidence_metrics": presentation_evidence_metrics(
                outline,
                confirmed["source_count"],
                records,
                list(inputs.get("source_document_ids") or []),
            ),
            "request_id": inputs.get("request_id"),
            "plan_task_id": inputs.get("plan_task_id"),
            "plan_digest": inputs.get("plan_digest"),
            **({"experiment_comparison": confirmed["experiment_comparison"],
                "measurement_evidence": build_experiment_evidence(confirmed["experiment_comparison"])}
               if confirmed.get("experiment_comparison") else {}),
            "elapsed_ms": elapsed_ms,
        }

    def _sync_presentation_artifact(self, inputs: dict[str, Any], task: dict[str, Any]) -> None:
        artifact_id = str(inputs.get("artifact_id") or "").strip()
        if not artifact_id:
            return
        catalog = ProjectCatalog(self.workspace)
        try:
            result = dict(task.get("result") or {})
            if task.get("kind") == "presentation.plan" and task.get("status") == "completed":
                catalog.update_artifact_provenance(
                    artifact_id,
                    source_version_ids=list(result.get("source_version_ids") or []),
                    plan_patch={
                        "plan_task_id": inputs.get("task_id"),
                        "plan_digest": result.get("plan_digest"),
                        "outline": result.get("outline") or {},
                        "source_refs": result.get("source_refs") or [],
                        "source_fingerprints": result.get("source_fingerprints") or {},
                        "evidence_metrics": result.get("evidence_metrics") or {},
                    },
                    confirmation_record_patch={
                        "status": "draft",
                        "generation_status": "plan_ready",
                        "plan_digest": result.get("plan_digest"),
                    },
                    change_summary="实际 PPT 大纲已生成并持久化；等待用户确认精确大纲摘要后才会生成文件。",
                )
            elif task.get("kind") == "presentation.plan" and task.get("status") == "cancelled":
                catalog.update_artifact_provenance(
                    artifact_id,
                    confirmation_record_patch={
                        "generation_status": "cancelled",
                        "failure_reason": "presentation_plan_cancelled",
                    },
                    change_summary="PPT 大纲任务已取消；未获得文件生成资格。",
                )
            elif task.get("kind") == "presentation.generate" and task.get("status") == "completed":
                metrics = dict(result.get("evidence_metrics") or {})
                catalog.update_artifact_provenance(
                    artifact_id,
                    source_version_ids=list(result.get("source_version_ids") or []),
                    plan_patch={
                        "generation_task_id": inputs.get("task_id"),
                        "output_path": result.get("output_path"),
                        "slide_count": result.get("slide_count"),
                        "renderer": result.get("renderer"),
                        "outline_source": result.get("outline_source"),
                        "source_refs": result.get("source_refs") or [],
                        "evidence_metrics": metrics,
                        "skill_application": result.get("skill_application") or {},
                        "skill_execution": result.get("skill_execution"),
                        "skill_gate_passed": result.get("skill_gate_passed"),
                    },
                    confirmation_record_patch={
                        "generation_status": "completed",
                        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                    change_summary=(
                        "首次生成已完成；已绑定来源版本并记录生成结果。"
                        f"证据页覆盖 {round(float(metrics.get('evidence_slide_coverage') or 0) * 100)}%，"
                        f"来源覆盖 {round(float(metrics.get('selected_source_coverage') or 0) * 100)}%。"
                    ),
                )
            elif task.get("kind") == "presentation.generate" and task.get("status") == "cancelled":
                catalog.update_artifact_provenance(
                    artifact_id,
                    confirmation_record_patch={
                        "generation_status": "cancelled",
                        "failure_reason": "presentation_generation_cancelled",
                    },
                    change_summary="PPT 生成任务已取消；已保留当前已有输出，未删除本地文件。",
                )
            else:
                catalog.update_artifact_provenance(
                    artifact_id,
                    confirmation_record_patch={
                        "generation_status": "failed",
                        "failure_reason": "presentation_task_failed",
                        "status_changed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    },
                    change_summary="生成任务未完成；已记录失败状态，未把失败结果标为可交付。",
                )
        except Exception as exc:
            self._audit(
                "presentation.artifact_provenance_failed",
                task,
                {"artifact_id": artifact_id, "error_type": type(exc).__name__},
            )
        finally:
            catalog.close()

    def _collect_evidence_step(
        self,
        inputs: dict[str, Any],
        _outputs: dict[str, dict[str, Any]],
        _step_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = inputs["source_filenames"]
        records = collect_indexed_evidence(
            self.workspace,
            self.keys,
            selected,
            project_id=inputs.get("project_id"),
            source_version_ids=inputs.get("source_version_ids"),
            source_document_ids=inputs.get("source_document_ids"),
        )
        comparison = self._load_experiment_comparison(inputs)
        if comparison is not None:
            required_versions = {str(comparison[role].get("evidence_version_id") or "") for role in ("baseline", "current")}
            found_versions = {str(record.get("asset_version_id") or "") for record in records}
            if "" in required_versions or not required_versions.issubset(found_versions):
                raise TaskPublicError("所选文档必须覆盖两次实验运行的准确来源版本")
        selected_ids = list(inputs.get("source_document_ids") or [])
        if selected_ids:
            found_ids = {
                str(record.get("document_id") or "").strip()
                for record in records
                if str(record.get("document_id") or "").strip()
            }
            missing_ids = [
                document_id
                for document_id in selected_ids
                if not _document_id_matches_value(document_id, found_ids)
            ]
            if missing_ids:
                raise TaskPublicError(
                    "selected document IDs are not indexed: "
                    + ", ".join(missing_ids[:3])
                )
            mismatched_labels = []
            for index, document_id in enumerate(selected_ids):
                expected_name = Path(str(selected[index] if index < len(selected) else "")).name.casefold()
                actual_names = {
                    Path(str(record.get("source_filename") or "")).name.casefold()
                    for record in records
                    if _document_id_matches_value(
                        document_id,
                        {str(record.get("document_id") or "")},
                    )
                }
                if actual_names and expected_name not in actual_names:
                    mismatched_labels.append(document_id)
            if mismatched_labels:
                raise TaskPublicError(
                    "source_document_ids do not match their display labels: "
                    + ", ".join(mismatched_labels[:3])
                )
        else:
            ambiguous_sources = []
            for source in selected:
                identities = {
                    str(record.get("document_id") or "").strip()
                    for record in records
                    if Path(str(record.get("source_filename") or "")).name.casefold() == source
                    and str(record.get("document_id") or "").strip()
                }
                if len(identities) > 1:
                    ambiguous_sources.append(source)
            if ambiguous_sources:
                raise TaskPublicError(
                    "document name is ambiguous; select exact document IDs: "
                    + ", ".join(ambiguous_sources[:3])
                )
        found_sources = {record["source_filename"] for record in records}
        missing = [source for source in selected if source not in found_sources]
        if missing:
            raise TaskPublicError(
                f"selected documents are not indexed: {', '.join(missing[:3])}"
            )
        return {
            "records": records,
            "source_count": len(selected_ids) if selected_ids else len(found_sources),
            "record_count": len(records),
        }

    def _build_outline_step(
        self,
        inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        _step_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        records = outputs["collect_evidence"]["records"]
        if inputs.get("experiment_comparison"):
            # A source citation does not validate a model's extra claims about
            # hardware or causality. Keep this bounded workflow extractive.
            return self._with_experiment_summary({
                **build_extractable_presentation_plan([], inputs),
                "outline_source": "experiment_ledger_extractive",
            }, inputs)
        if self.outline_generator is not None:
            try:
                generated = self.outline_generator(records, inputs)
                outline_source = str(generated.get("outline_source") or "custom_generator")
                if outline_source not in {"local_ollama", "extractive", "custom_generator"}:
                    outline_source = "custom_generator"
                outline = validate_presentation_plan(generated, inputs)
                validate_generated_presentation_evidence(generated, records, inputs)
                validate_generated_presentation_evidence(outline, records, inputs)
                return self._with_experiment_summary({
                    **outline,
                    "outline_source": outline_source,
                    "evidence_validation": {
                        "status": "source_bound_excerpts",
                        "notice": "正文仅保留所列来源的原文摘录；不证明跨材料可比性、因果或原始资料本身正确。",
                    },
                }, inputs)
            except Exception:
                pass
        outline = build_extractable_presentation_plan(records, inputs)
        for slide in outline["slides"]:
            if slide["kind"] == "cover":
                slide["subtitle"] = _compact(slide["subtitle"] + "；原文摘录，不据此判定可比性或因果", 100)
            elif slide["kind"] == "content":
                slide["footer"] = _compact("原文摘录 · 可比性/因果未验证 · " + slide["footer"], 90)
        return self._with_experiment_summary({
            **outline,
            "outline_source": "extractive",
            "evidence_validation": {
                "status": "extractive_fallback" if self.outline_generator is not None else "extractive",
                "notice": (
                    "自动提纲未完成原文绑定校验，已改用来源摘录；不证明跨材料可比性、因果或原始资料本身正确。"
                    if self.outline_generator is not None
                    else "正文为来源摘录；不证明跨材料可比性、因果或原始资料本身正确。"
                ),
            },
        }, inputs)

    @staticmethod
    def _with_experiment_summary(outline: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        measurements = build_experiment_evidence(inputs.get("experiment_comparison"))
        if not measurements:
            return outline
        comparison = inputs["experiment_comparison"]
        if len(measurements) != len(comparison.get("changes") or []):
            raise TaskPublicError("所选指标超出当前完整证据预算，请缩小比较范围后重新生成大纲")
        slides = [outline["slides"][0]]
        for offset in range(0, len(measurements), 4):
            points = [
                f"{item['metric']}：{item['baseline_value']:.12g}{item['unit']} → {item['current_value']:.12g}{item['unit']}；"
                f"绝对变化 {item['absolute_delta']:+.12g}{item['absolute_delta_unit']} [{item['id']}]"
                for item in measurements[offset:offset + 4]
            ]
            points.append("配置/环境如有差异，仅为候选因素，不能单独证明因果；需单因素验证。")
            slides.append(_content_slide("已确认实验测量对比", points, inputs["source_filenames"],
                                         "M# 来自所选运行的可比账本，完整来源与定位随计划保存"))
        baseline, current = comparison["baseline"], comparison["current"]
        conditions = _content_slide("所选运行的记录条件", [
            f"{label}：基线 {baseline.get(field) or '未记录'}；当前 {current.get(field) or '未记录'}"
            for field, label in (("performed_at", "执行时间"), ("dataset_version", "数据集版本"),
                                 ("evaluation_script_ref", "评测脚本"), ("config_ref", "配置引用"),
                                 ("environment_ref", "环境引用"))
        ], inputs["source_filenames"], "仅展示账本引用，不据此推定硬件变化或因果关系")
        slides.extend([conditions, outline["slides"][-1]])
        if len(slides) > inputs["max_slides"]:
            raise TaskPublicError("页数上限不足以完整展示所选指标，请提高上限后重新生成大纲")
        return {**outline, "slides": slides}

    @staticmethod
    def _require_experiment_outline(inputs: dict[str, Any], result: dict[str, Any]) -> None:
        if inputs.get("baseline_run_id") and (result.get("outline") or {}).get("outline_source") != "experiment_ledger_extractive":
            raise ValueError("旧实验比较大纲含未经账本约束的内容，请重新生成并确认")

    def _load_confirmed_plan_step(
        self,
        inputs: dict[str, Any],
        _outputs: dict[str, dict[str, Any]],
        _step_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        journal = TaskJournal(self.workspace, self.keys)
        try:
            plan_task = journal.get_task(str(inputs.get("plan_task_id") or ""))
        finally:
            journal.close()
        if plan_task["kind"] != "presentation.plan" or plan_task["status"] != "completed":
            raise ValueError("PPT 大纲任务尚未完成")
        plan_result = dict(plan_task.get("result") or {})
        if str(plan_result.get("plan_digest") or "") != str(inputs.get("plan_digest") or ""):
            raise ValueError("PPT 大纲摘要不匹配，请重新确认")
        self._require_experiment_outline(inputs, plan_result)
        _require_generated_outline_evidence(inputs, plan_result)
        self._load_experiment_comparison(inputs)
        snapshot = dict(plan_result.get("evidence_snapshot") or {})
        records = list(snapshot.get("records") or [])
        if not records or snapshot.get("source_fingerprints") != _source_fingerprints(
            collect_indexed_evidence(
                self.workspace,
                self.keys,
                list(inputs.get("source_filenames") or []),
                project_id=inputs.get("project_id"),
                source_version_ids=inputs.get("source_version_ids"),
                source_document_ids=inputs.get("source_document_ids"),
            ),
            list(inputs.get("source_filenames") or []),
            list(inputs.get("source_document_ids") or []),
        ):
            raise ValueError("所选证据已变化，请重新生成大纲")
        artifact_id = str(inputs.get("artifact_id") or "").strip()
        if artifact_id:
            catalog = ProjectCatalog(self.workspace)
            try:
                artifact = catalog.get_artifact(artifact_id)
            finally:
                catalog.close()
            if artifact is None or artifact.get("confirmation_status") != "confirmed":
                raise ValueError("PPT 大纲尚未确认")
        outline = dict(plan_result.get("outline") or {})
        return {
            "plan": outline,
            "records": records,
            "source_count": int(plan_result.get("source_count") or len({record.get("source_filename") for record in records})),
            "source_document_ids": list(plan_result.get("source_document_ids") or inputs.get("source_document_ids") or []),
            "source_version_ids": list(plan_result.get("source_version_ids") or []),
            "source_refs": list(plan_result.get("source_refs") or []),
            "source_fingerprints": dict(snapshot.get("source_fingerprints") or {}),
            **({"experiment_comparison": inputs["experiment_comparison"]} if inputs.get("experiment_comparison") else {}),
        }

    def _render_pptx_step(
        self,
        inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        step_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = outputs.get("load_confirmed_plan", {}).get("plan") or outputs.get("build_outline")
        if not plan:
            raise ValueError("confirmed presentation plan is missing")
        output_directory = Path(self.workspace.presentations_path).resolve()
        output_directory.mkdir(parents=True, exist_ok=True)
        filename = presentation_filename(
            inputs["title"],
            inputs["source_filenames"],
            inputs["task_id"],
        )
        output_path = output_directory / filename
        expected_slide_count = len(plan["slides"])
        marker_path = Path(f"{output_path}.souldrive.json")
        step_idempotency_key = str((step_context or {}).get("idempotency_key") or "")
        marker_payload = {
            "task_id": inputs.get("task_id"),
            "plan_digest": _presentation_plan_digest(plan),
            "step_idempotency_key": step_idempotency_key,
            "slide_count": expected_slide_count,
        }
        if inputs.get("plan_digest") and inputs["plan_digest"] != marker_payload["plan_digest"]:
            raise TaskPublicError("实际 PPT 大纲与确认版本不一致，请重新创建并确认计划。")
        if not output_path.exists():
            legacy_suffix = re.sub(r"[^A-Za-z0-9]", "", str(inputs["task_id"]))[-8:] or "task"
            legacy_path = output_directory / f"{filename.rsplit('-', 1)[0]}-{legacy_suffix}.pptx"
            if legacy_path.exists():
                try:
                    legacy_marker = json.loads(Path(f"{legacy_path}.souldrive.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    legacy_marker = {}
                if isinstance(legacy_marker, dict) and legacy_marker.get("task_id") == inputs["task_id"]:
                    raise TaskPublicError("此任务已有旧版 PPT 产物，已保留；缺少内容完成指纹，请重新创建并确认计划。")
        marker = {}
        if marker_path.exists():
            try:
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                if not isinstance(marker, dict):
                    marker = {}
            except (OSError, ValueError):
                marker = {}
        matches = all(marker.get(key) == value for key, value in marker_payload.items())
        staged_name = str(marker.get("staged_name") or "")
        staged_path = (
            output_directory / staged_name
            if Path(staged_name).name == staged_name and staged_name.startswith(f".{filename}.")
            else None
        )
        if output_path.exists():
            if not (
                matches
                and marker.get("status") in {"ready", "completed"}
                and marker.get("content_sha256")
                and _presentation_file_digest(output_path) == marker["content_sha256"]
                and verify_pptx(output_path, expected_slide_count)["valid"]
            ):
                raise TaskPublicError("PPT 路径已有无法验证或已修改的文件，已保留且未覆盖；请重新创建并确认计划。")
            _write_json_marker(marker_path, {**marker, "status": "completed"})
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
            return {
                "output_path": str(output_path),
                "renderer": "recovered",
                "planned_slide_count": expected_slide_count,
                "marker_path": str(marker_path),
            }
        if matches and marker.get("status") == "ready":
            if staged_path is None:
                raise TaskPublicError("PPT 暂存文件身份无法验证，已保留现有文件；请重新创建并确认计划。")
            _publish_presentation_file(staged_path, output_path, marker_path, marker)
            return {
                "output_path": str(output_path),
                "renderer": "recovered",
                "planned_slide_count": expected_slide_count,
                "marker_path": str(marker_path),
            }
        with tempfile.NamedTemporaryFile(
            prefix=f".{filename}.", suffix=".staging.pptx", dir=output_directory, delete=False,
        ) as staged:
            staged_path = Path(staged.name)
        marker_payload["staged_name"] = staged_path.name
        _write_json_marker(marker_path, {**marker_payload, "status": "rendering"})
        if os.environ.get("SOULDRIVE_PRESENTATION_RENDERER", "openxml").strip().lower() != "com":
            payload = render_presentation(plan, staged_path)
        else:
            completed = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(resolve_wps_render_script()),
                    "-OutputPath", str(staged_path),
                ],
                input=json.dumps(plan, ensure_ascii=False),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=180,
            )
            if completed.returncode != 0:
                error = (completed.stderr or completed.stdout or "WPS presentation rendering failed").strip()
                raise RuntimeError(error[-1000:])
            payload = _last_json_object(completed.stdout)
        if not verify_pptx(staged_path, expected_slide_count)["valid"]:
            raise TaskPublicError("PPT 暂存文件未通过结构校验，未发布或覆盖现有文件。")
        with staged_path.open("rb+") as staged:
            os.fsync(staged.fileno())
        marker_payload.update({
            "status": "ready",
            "content_sha256": _presentation_file_digest(staged_path),
        })
        _write_json_marker(marker_path, marker_payload)
        _publish_presentation_file(staged_path, output_path, marker_path, marker_payload)
        return {
            "output_path": str(output_path),
            "renderer": str(payload.get("renderer") or "WPS"),
            "planned_slide_count": expected_slide_count,
            "marker_path": str(marker_path),
        }

    def _verify_pptx_step(
        self,
        _inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        _step_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        output_path = Path(outputs["render_pptx"]["output_path"])
        expected = int(outputs["render_pptx"]["planned_slide_count"])
        report = verify_pptx(output_path, expected)
        if not report["valid"]:
            raise RuntimeError("generated PPTX failed validation: " + ", ".join(report["errors"]))
        report["skill_execution"] = None
        report["skill_gate_passed"] = True
        return report

    def _open_in_wps_step(
        self,
        inputs: dict[str, Any],
        outputs: dict[str, dict[str, Any]],
        step_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not inputs.get("open_in_wps"):
            return {"opened": False, "reason": "not_requested"}
        output_path = outputs["render_pptx"]["output_path"]
        marker = Path(self.workspace.runtime_path) / f"{inputs['task_id']}.wps-opened"
        if marker.exists():
            return {
                "opened": False,
                "reason": "already_requested",
                "step_idempotency_key": str((step_context or {}).get("idempotency_key") or ""),
            }
        executable = find_wps_presentation_executable()
        if not executable:
            raise RuntimeError("WPS presentation executable was not found")
        _write_json_marker(
            marker,
            {
                "requested_at": time.time(),
                "output_path": output_path,
                "step_idempotency_key": str((step_context or {}).get("idempotency_key") or ""),
            },
        )
        try:
            subprocess.Popen(
                [executable, output_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            marker.unlink(missing_ok=True)
            raise
        return {
            "opened": True,
            "application": "WPS",
            "executable_name": Path(executable).name,
            "step_idempotency_key": str((step_context or {}).get("idempotency_key") or ""),
        }

    def _audit(self, event_type: str, task: dict[str, Any], extra: dict[str, Any]) -> None:
        if self.audit_logger is None:
            return
        append_audit_event_best_effort(
            self.audit_logger,
            event_type,
            {
                "task_id": task.get("task_id"),
                "status": task.get("status"),
                **extra,
            },
        )


def _presentation_file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _publish_presentation_file(
    staged_path: Path,
    output_path: Path,
    marker_path: Path,
    marker: dict[str, Any],
) -> None:
    if not (
        staged_path.is_file()
        and marker.get("content_sha256")
        and _presentation_file_digest(staged_path) == marker["content_sha256"]
        and verify_pptx(staged_path, int(marker["slide_count"]))["valid"]
    ):
        raise TaskPublicError("PPT 暂存文件已改变或无法验证，已保留现有文件；请重新创建并确认计划。")
    # A hard link publishes the verified bytes atomically without replacing an
    # existing destination.  Both paths are on the same local filesystem.
    try:
        os.link(staged_path, output_path)
    except FileExistsError as exc:
        raise TaskPublicError("PPT 路径已有文件，已保留且未覆盖；请重新创建并确认计划。") from exc
    _write_json_marker(marker_path, {**marker, "status": "completed"})
    staged_path.unlink(missing_ok=True)


def _write_json_marker(path: Path, payload: dict[str, Any]) -> None:
    """Publish a complete recovery marker without exposing a partial JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _stable_presentation_task_id(kind: str, identity: str) -> str:
    digest = hashlib.sha256(f"{kind}:{identity}".encode("utf-8")).hexdigest()
    return f"task_{digest[:24]}"


def _presentation_request_digest(inputs: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in inputs.items()
        if key not in {"request_id", "task_id", "artifact_id", "request_digest"}
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _presentation_repetition_inputs(
    inputs: dict[str, Any],
    *,
    plan_digest: str | None = None,
) -> dict[str, Any]:
    """Project PPT business identity away from per-execution identifiers."""
    projection = {
        "project_id": inputs.get("project_id"),
        "source_filenames": list(inputs.get("source_filenames") or []),
        "source_document_ids": list(inputs.get("source_document_ids") or []),
        "source_version_ids": list(inputs.get("source_version_ids") or []),
        "title": inputs.get("title"),
        "audience": inputs.get("audience"),
        "max_slides": inputs.get("max_slides"),
        "open_in_wps": bool(inputs.get("open_in_wps")),
        "style_skill_id": inputs.get("style_skill_id"),
    }
    if "style_skill" in inputs:
        projection["style_skill"] = inputs.get("style_skill")
    if inputs.get("baseline_run_id"):
        projection.update({key: inputs[key] for key in ("baseline_run_id", "current_run_id")})
        projection["comparison_digest"] = _presentation_plan_digest(inputs.get("experiment_comparison") or {})
    if plan_digest is not None:
        projection["plan_digest"] = str(plan_digest)
    return projection


def _presentation_plan_digest(plan: dict[str, Any]) -> str:
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _presentation_source_refs(
    inputs: dict[str, Any],
    records: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    version_ids = sorted({
        str(record.get("asset_version_id"))
        for record in records
        if str(record.get("asset_version_id") or "").strip()
    })
    source_refs = []
    selected_ids = list(inputs.get("source_document_ids") or [])
    for index, source in enumerate(inputs["source_filenames"]):
        selected_id = selected_ids[index] if index < len(selected_ids) else ""
        source_version_ids = sorted({
            str(record.get("asset_version_id"))
            for record in records
            if (
                _document_id_matches_value(selected_id, {str(record.get("document_id") or "")})
                if selected_id
                else record.get("source_filename") == source
            )
            and str(record.get("asset_version_id") or "").strip()
        })
        source_refs.append({
            "source_filename": source,
            "document_id": selected_id or None,
            "version_ids": source_version_ids,
            "status": "resolved" if source_version_ids else "unresolved",
        })
    return version_ids, source_refs


def _source_fingerprints(
    records: list[dict[str, Any]],
    selected_sources: list[str],
    selected_document_ids: list[str] | None = None,
) -> dict[str, str]:
    fingerprints: dict[str, str] = {}
    selected_ids = list(selected_document_ids or [])
    for index, source in enumerate(selected_sources):
        selected_id = selected_ids[index] if index < len(selected_ids) else ""
        source_records = [
            record for record in records
            if (
                _document_id_matches_value(selected_id, {str(record.get("document_id") or "")})
                if selected_id
                else Path(str(record.get("source_filename") or "")).name == Path(source).name
            )
        ]
        canonical_records = [
            {
                "asset_id": str(record.get("asset_id") or ""),
                "asset_version_id": str(record.get("asset_version_id") or ""),
                "parent_id": str(record.get("parent_id") or ""),
                "page_number": record.get("page_number"),
                "parent_index": record.get("parent_index"),
                "section": str(record.get("section") or ""),
                "content": str(record.get("content") or ""),
            }
            for record in source_records
        ]
        canonical_records.sort(
            key=lambda record: (
                str(record["asset_version_id"]),
                str(record["parent_id"]),
                int(record["page_number"] or 0),
                int(record["parent_index"] or 0),
                record["content"],
            )
        )
        encoded = json.dumps(canonical_records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = selected_id or Path(source).name
        fingerprints[key] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return fingerprints


def _document_id_candidates(value: object) -> set[str]:
    normalized = str(value or "").strip()
    if not normalized:
        return set()
    if normalized.startswith("secure_"):
        return {normalized, normalized.removeprefix("secure_")}
    return {normalized, f"secure_{normalized}"}


def _document_id_matches_value(value: object, actual_ids: set[str]) -> bool:
    expected = _document_id_candidates(value)
    if not expected:
        return False
    for actual_id in actual_ids:
        if _document_id_candidates(actual_id) & expected:
            return True
    return False


def validate_presentation_request(request: dict[str, Any]) -> dict[str, Any]:
    raw_source_values = list(request.get("source_filenames") or [])
    source_document_ids = []
    seen_document_ids = set()
    seen_document_id_aliases = set()
    for value in request.get("source_document_ids") or []:
        document_id = str(value or "").strip()[:256]
        if not document_id:
            continue
        aliases = _document_id_candidates(document_id)
        if document_id in seen_document_ids or aliases & seen_document_id_aliases:
            raise ValueError("source_document_ids must identify distinct documents")
        source_document_ids.append(document_id)
        seen_document_ids.add(document_id)
        seen_document_id_aliases.update(aliases)
    sources = []
    seen_source_keys = set()
    for value in raw_source_values:
        source = Path(str(value or "").strip()).name
        source_key = source.casefold()
        if source and (source_document_ids or source_key not in seen_source_keys):
            sources.append(source)
            seen_source_keys.add(source_key)
    if source_document_ids and not raw_source_values:
        raise ValueError("source_filenames are required as display labels with source_document_ids")
    if source_document_ids and len(source_document_ids) != len(sources):
        raise ValueError("source_filenames and source_document_ids must have the same length")
    if not 1 <= len(sources) <= 12:
        raise ValueError("select between 1 and 12 indexed documents")
    title = " ".join(str(request.get("title") or "").split())[:120]
    audience = str(request.get("audience") or "research").strip().lower()
    if audience not in ALLOWED_AUDIENCES:
        raise ValueError("unsupported presentation audience")
    try:
        max_slides = int(request.get("max_slides") or 10)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_slides must be an integer") from exc
    max_slides = max(4, min(max_slides, 16))
    project_id = str(request.get("project_id") or "").strip()[:128]
    baseline_run_id = str(request.get("baseline_run_id") or "").strip()[:128]
    current_run_id = str(request.get("current_run_id") or "").strip()[:128]
    if bool(baseline_run_id) != bool(current_run_id) or (baseline_run_id and not project_id):
        raise ValueError("实验比较 PPT 需要当前项目和完整的基线/当前运行对")
    source_version_ids = []
    for value in request.get("source_version_ids") or []:
        source_version_id = str(value or "").strip()
        if source_version_id and source_version_id not in source_version_ids:
            source_version_ids.append(source_version_id[:160])
    request_id = str(request.get("request_id") or "").strip()[:128] or f"req_{uuid.uuid4().hex}"
    return {
        "request_id": request_id,
        "source_filenames": sources,
        "source_document_ids": source_document_ids[:12],
        "title": title,
        "audience": audience,
        "max_slides": max_slides,
        "open_in_wps": bool(request.get("open_in_wps", True)),
        "style_skill_id": str(request.get("style_skill_id") or "").strip(),
        "project_id": project_id,
        "source_version_ids": source_version_ids[:64],
        **({"baseline_run_id": baseline_run_id, "current_run_id": current_run_id} if baseline_run_id else {}),
    }


def collect_indexed_evidence(
    workspace,
    keys,
    selected_sources: list[str] | None,
    *,
    project_id: str | None = None,
    source_version_ids: list[str] | None = None,
    source_document_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    selected_sources = list(selected_sources or [])
    selected = {Path(source).name.casefold(): Path(source).name for source in selected_sources}
    selected_document_ids = [
        str(document_id or "").strip()
        for document_id in source_document_ids or []
        if str(document_id or "").strip()
    ]
    selected_id_keys = {
        document_id
        for document_id in selected_document_ids
    }

    def matching_document_id(metadata: dict[str, Any]) -> str | None:
        actual_id = str((metadata or {}).get("document_id") or "").strip()
        if not actual_id:
            return None
        for requested_id in selected_document_ids:
            if _document_id_matches_value(requested_id, {actual_id}):
                return requested_id
        return None

    def selected_source_name(metadata: dict[str, Any]) -> str | None:
        source = Path(str((metadata or {}).get("source_filename") or "")).name
        source_key = source.casefold()
        if selected_document_ids:
            if matching_document_id(metadata) is None:
                return None
        elif selected and source_key not in selected:
            return None
        return selected.get(source_key) or source or None

    scoped_project_id = str(project_id or "").strip()
    scoped_version_ids = {
        str(version_id).strip()
        for version_id in source_version_ids or []
        if str(version_id).strip()
    }
    records: list[dict[str, Any]] = []
    known_parent_ids: set[str] = set()
    # Consult the durable catalog even for a workspace-global presentation.
    # A legacy parent row without project_id must not become global merely
    # because the caller omitted project_id when selecting by filename.
    catalog_projects_by_source = _catalog_projects_by_source(workspace, set(selected))
    secure_path = Path(workspace.secure_vector_store_path)
    if keys is not None and secure_path.exists():
        store = SecureVectorStore(str(secure_path), keys)
        try:
            payload = store.collection.get(include=["metadatas"])
            parent_ids: dict[str, dict[str, Any]] = {}
            for metadata in payload.get("metadatas") or []:
                parent_id = str((metadata or {}).get("parent_id") or "")
                if selected_source_name(metadata) and parent_id:
                    parent_ids.setdefault(parent_id, dict(metadata or {}))
            for parent_id, child_metadata in parent_ids.items():
                parent = store.get_parent(parent_id)
                if parent:
                    known_parent_ids.add(parent_id)
                    metadata = _merge_evidence_metadata(
                        child_metadata,
                        parent.get("metadata") if isinstance(parent, dict) else {},
                    )
                    if metadata is None:
                        # Child and parent indexes disagree about provenance.
                        # Do not let the less trustworthy merge order turn a
                        # foreign project record into an apparently local one.
                        continue
                    source_name = selected_source_name(metadata)
                    if not source_name:
                        continue
                    source_key = Path(source_name).name.casefold()
                    if not _catalog_scope_matches(
                        metadata,
                        source_key,
                        scoped_project_id,
                        catalog_projects_by_source,
                    ) or not _evidence_matches_scope(metadata, scoped_project_id, scoped_version_ids):
                        continue
                    record = _evidence_record(parent_id, parent["content"], metadata)
                    record["source_filename"] = source_name
                    records.append(record)
        finally:
            store.close()

    # The document library also accepts a durable parent index as evidence of
    # indexing.  A vector rebuild can temporarily lag behind that index (or a
    # plain document may intentionally have no vector row), so use the parent
    # store for sources not found above instead of reporting a false failure.
    found_document_ids = {
        matching_document_id(record)
        for record in records
        if matching_document_id(record)
    }
    missing_sources = set(selected) - {
        Path(record["source_filename"]).name.casefold() for record in records
    }
    missing_document_ids = selected_id_keys - found_document_ids
    if missing_sources or missing_document_ids or not records:
        db_path = Path(workspace.parent_doc_path)
        if not db_path.exists():
            if records:
                return records
            raise FileNotFoundError("parent document index is not available")
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT parent_id, content, metadata_json FROM parent_documents"
            ).fetchall()
        finally:
            conn.close()
        for parent_id, content, metadata_json in rows:
            if parent_id in known_parent_ids:
                continue
            metadata = json.loads(metadata_json)
            source_name = selected_source_name(metadata)
            source_key = Path(source_name).name.casefold() if source_name else ""
            if (
                source_name
                and (
                    source_key in missing_sources
                    or bool(missing_document_ids)
                )
                and _catalog_scope_matches(
                    metadata,
                    source_key,
                    scoped_project_id,
                    catalog_projects_by_source,
                )
                and _evidence_matches_scope(metadata, scoped_project_id, scoped_version_ids)
            ):
                record = _evidence_record(parent_id, content, metadata)
                record["source_filename"] = source_name
                records.append(record)

    source_order = {
        Path(source).name.casefold(): index
        for index, source in enumerate(selected_sources)
    }
    id_order = {
        document_id: index
        for index, document_id in enumerate(selected_document_ids)
    }

    def record_order(item: dict[str, Any]) -> tuple[int, int, int]:
        matched_id = matching_document_id(item)
        return (
            id_order.get(matched_id, source_order.get(
                Path(str(item.get("source_filename") or "")).name.casefold(),
                len(selected_sources) + len(selected_document_ids),
            )),
            int(item.get("page_number") or 0),
            int(item.get("parent_index") or 0),
        )

    records.sort(key=record_order)
    limited = []
    per_source: Counter[str] = Counter()
    for record in records:
        limit_key = (
            matching_document_id(record)
            or record["source_filename"]
        )
        if per_source[limit_key] >= 18:
            continue
        if len(limited) >= 96:
            break
        limited.append(record)
        per_source[limit_key] += 1
    return limited


def _evidence_matches_scope(
    metadata: dict[str, Any],
    project_id: str,
    source_version_ids: set[str],
) -> bool:
    metadata_project_id = str(metadata.get("project_id") or "").strip()
    # An absent project_id is the durable representation of a shared/global
    # document.  It is available to a selected project, while a project-bound
    # document requires an explicit matching project scope.  This prevents the
    # legacy filename-only entry point from merging same-named private files.
    if metadata_project_id and not project_id:
        return False
    if project_id and metadata_project_id and metadata_project_id != project_id:
        return False
    if source_version_ids:
        version_id = str(
            metadata.get("asset_version_id") or metadata.get("version_id") or ""
        ).strip()
        if version_id not in source_version_ids:
            return False
    return True


def _catalog_projects_by_source(
    workspace,
    selected_sources: set[str],
) -> dict[str, set[str]]:
    """Return durable project bindings for selected filenames.

    An old parent index can omit ``project_id``.  If the catalog still proves
    that the filename is project-bound, treating that row as global would make
    a filename-only presentation selection unsafe.
    """
    if not selected_sources or not getattr(workspace, "project_catalog_path", None):
        return {}
    catalog = ProjectCatalog(workspace)
    try:
        rows = catalog.conn.execute(
            """
            SELECT a.project_id, v.file_ref
            FROM assets a
            JOIN asset_versions v ON v.asset_id = a.asset_id
            """
        ).fetchall()
    finally:
        catalog.close()
    result: dict[str, set[str]] = {}
    for row in rows:
        source_key = Path(str(row[1] or "")).name.casefold()
        project_id = str(row[0] or "").strip()
        if source_key in selected_sources and project_id:
            result.setdefault(source_key, set()).add(project_id)
    return result


def _catalog_scope_matches(
    metadata: dict[str, Any],
    source_key: str,
    scoped_project_id: str,
    catalog_projects_by_source: dict[str, set[str]],
) -> bool:
    bound_projects = catalog_projects_by_source.get(source_key)
    if not bound_projects:
        return True
    stored_project_id = str(metadata.get("project_id") or "").strip()
    if not scoped_project_id:
        return False
    return bool(
        stored_project_id
        and stored_project_id == scoped_project_id
        and stored_project_id in bound_projects
    )


_EVIDENCE_PROVENANCE_FIELDS = (
    "source_filename",
    "project_id",
    "asset_id",
    "asset_version_id",
    "document_id",
    "document_hash",
    "catalog_file_ref",
)


def _merge_evidence_metadata(
    child_metadata: dict[str, Any] | None,
    parent_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Merge child/parent provenance without allowing identity downgrades."""
    child = dict(child_metadata or {})
    parent = dict(parent_metadata or {})
    for field in _EVIDENCE_PROVENANCE_FIELDS:
        child_value = str(child.get(field) or "").strip()
        parent_value = str(parent.get(field) or "").strip()
        if child_value and parent_value and child_value != parent_value:
            return None
    merged = dict(child)
    for key, value in parent.items():
        if not str(merged.get(key) or "").strip() and value is not None:
            merged[key] = value
    return merged


def build_extractable_presentation_plan(
    records: list[dict[str, Any]],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    sources = inputs["source_filenames"]
    by_source = {source: [] for source in sources}
    for record in records:
        by_source.setdefault(record["source_filename"], []).append(record)

    title = inputs["title"] or _default_title(sources)
    skill = dict(inputs.get("style_skill") or {})
    skill_contract = _presentation_contract_for_skill(skill)
    point_limit = int(skill_contract.get("point_limit") or 5)
    footer_suffix = f" · 本地契约：{skill_contract['adaptation']}" if skill else ""
    audience_label = {
        "research": "高校论文与实验汇报",
        "technical": "技术方案汇报",
        "government": "政策与项目汇报",
    }[inputs["audience"]]
    slides: list[dict[str, Any]] = [
        {
            "kind": "cover",
            "eyebrow": audience_label,
            "title": _compact(title, 54),
            "subtitle": _compact(f"基于 {len(sources)} 份已入库文档生成{footer_suffix}", 80),
            "points": [],
            "sources": sources,
            "footer": "",
        }
    ]

    summary_points = []
    summary_sources = []
    for source in sources:
        sentences = _ranked_sentences(by_source.get(source) or [])
        if sentences:
            summary_points.append(f"{Path(source).stem}：{sentences[0]}")
            summary_sources.append(source)
    slides.append(
        _content_slide(
            "执行摘要",
            summary_points[:min(point_limit, 4)] or ["所选文档缺少可用于摘要的结构化正文。"],
            summary_sources[:4] or sources[:4],
            _compact(f"摘要内容直接提炼自所选文档{footer_suffix}", 90),
        )
    )

    remaining_content_slots = max(1, inputs["max_slides"] - 3)
    section_sequence = skill_contract.get("section_sequence") if skill else []
    if section_sequence:
        slides.extend(_skill_stage_slides(records, sources, skill_contract, remaining_content_slots, point_limit, footer_suffix))
    else:
        source_slots = min(len(sources), remaining_content_slots)
        for source in sources[:source_slots]:
            source_records = by_source.get(source) or []
            points = _ranked_sentences(source_records)[:point_limit]
            section = next((item["section"] for item in source_records if item["section"]), "文档要点")
            slides.append(
                _content_slide(
                    _compact(Path(source).stem, 38),
                    points or ["未提取到足够的结构化正文。"],
                    [source],
                    _compact(f"{section}{footer_suffix}", 90),
                )
            )

    if len(sources) > 1 and len(slides) < inputs["max_slides"] - 1:
        common_terms = _common_terms([record for record in records if record["source_filename"] in sources[:4]], limit=5)
        observations = [f"共同关注主题：{term}" for term in common_terms]
        if not observations:
            observations = ["所选文档的共同主题需要结合原文证据进一步核对。"]
        slides.append(
            _content_slide(
                "跨文档综合观察",
                observations[:point_limit],
                sources,
                "仅呈现多文档中重复出现的主题词",
            )
        )

    slides = slides[: inputs["max_slides"] - 1]
    slides.append(
        {
            "kind": "sources",
            "title": "资料来源",
            "points": [],
            "sources": [_compact(source, 96) for source in sources],
            "footer": "所有内容均来自本地已入库文档",
        }
    )
    return validate_presentation_plan(
        {
            "schema_version": "souldrive_presentation_plan_v1",
            "generated_at_label": time.strftime("%Y-%m-%d %H:%M"),
            "title": title,
            "slides": slides,
        "style": _presentation_style_for_skill(inputs.get("style_skill")),
        "style_skill": skill,
        "presentation_contract": skill_contract,
            "skill_application": presentation_skill_application(inputs.get("style_skill")),
        },
        inputs,
    )


def _skill_stage_slides(
    records: list[dict[str, Any]],
    sources: list[str],
    contract: dict[str, Any],
    limit: int,
    point_limit: int,
    footer_suffix: str,
) -> list[dict[str, Any]]:
    stages = list(contract.get("section_sequence") or [])
    keywords = list(contract.get("stage_keywords") or [])
    result = []
    # Content slides can display at most three source labels in this route.
    records = [record for record in records if record["source_filename"] in sources[:3]]
    for index, stage in enumerate(stages[:limit]):
        terms = [str(item).casefold() for item in (keywords[index] if index < len(keywords) else [])]
        matched = [
            record for record in records
            if any(term in f"{record.get('section') or ''} {record.get('content') or ''}".casefold() for term in terms)
        ]
        selected = matched or records
        points = _ranked_sentences(selected)[:point_limit]
        result.append(
            _content_slide(
                _compact(str(stage), 50),
                points or [f"{stage}：当前资料未提供足够证据。"],
                sources[:3],
                _compact(f"按业务阶段组织；缺失证据需人工补充{footer_suffix}", 96),
            )
        )
    return result


def presentation_skill_application(skill: dict[str, Any] | None) -> dict[str, Any]:
    """Turn approved Skill instructions into the small set of safe PPT invariants we enforce."""
    skill = dict(skill or {})
    skill_id = str(skill.get("skill_id") or "").strip() or None
    output_contract = skill.get("output_contract") or []
    if isinstance(output_contract, (list, tuple)):
        output_contract_text = " ".join(str(item) for item in output_contract)
    else:
        output_contract_text = str(output_contract)
    content = " ".join(
        [
            str(skill.get("content") or ""),
            str(skill.get("applicability") or ""),
            " ".join(str(item) for item in skill.get("steps") or []),
            output_contract_text,
        ]
    ).lower()
    rules = []
    if any(term in content for term in ("source", "citation", "evidence", "引用", "来源")):
        rules.append("source_attribution")
    if any(term in content for term in ("pptx", "potx", "editable", "演示")):
        rules.append("editable_pptx")
    if any(term in content for term in ("validate", "qa", "quality", "校验", "检查")):
        rules.append("structural_qa")
    if any(term in content for term in ("visual", "layout", "slide", "视觉", "布局", "幻灯片")):
        rules.append("visual_layout")
    resources = [
        dict(item)
        for item in (skill.get("resource_manifest") or [])[:48]
        if isinstance(item, dict) and str(item.get("path") or "").strip() and str(item.get("kind") or "") != "script"
    ]
    style = _presentation_style(skill.get("presentation_style"))
    has_explicit_style = bool(isinstance(skill.get("presentation_style"), dict) and skill.get("presentation_style"))
    presentation_contract = _presentation_contract_for_skill(skill)
    script_count = 0
    return {
        "skill_id": skill_id,
        "skill_title": str(skill.get("title") or "").strip() or None,
        "mode": "approved_skill_constraints",
        "instructions_applied": bool(skill_id and (content or rules)),
        "applied_rules": rules,
        "style_applied": bool(skill_id and has_explicit_style),
        "style_source": str(skill.get("presentation_style_source") or "").strip() or None,
        "style": style if has_explicit_style else {},
        "presentation_contract": presentation_contract,
        "resource_manifest": resources,
        "script_count": script_count,
        "script_policy": "disabled",
    }


def validate_presentation_plan(plan: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict) or not isinstance(plan.get("slides"), list):
        raise ValueError("invalid presentation plan")
    selected = set(inputs["source_filenames"])
    slides = []
    for index, raw_slide in enumerate(plan["slides"][: inputs["max_slides"]]):
        if not isinstance(raw_slide, dict):
            continue
        kind = str(raw_slide.get("kind") or ("cover" if index == 0 else "content"))
        if kind not in {"cover", "content", "sources"}:
            kind = "content"
        title = _compact(raw_slide.get("title") or "未命名", 60)
        points = [
            _compact(point, 105)
            for point in raw_slide.get("points") or []
            if str(point or "").strip()
        ][:5]
        sources = [
            Path(str(source or "")).name
            for source in raw_slide.get("sources") or []
            if Path(str(source or "")).name in selected
        ]
        if kind != "cover" and not sources:
            sources = inputs["source_filenames"][:3]
        slides.append(
            {
                "kind": kind,
                "eyebrow": _compact(raw_slide.get("eyebrow") or "专题汇报", 40),
                "title": title,
                "subtitle": _compact(raw_slide.get("subtitle") or "", 100),
                "points": points,
                "sources": sources[:12] if kind == "sources" else sources[:4],
                "footer": _compact(raw_slide.get("footer") or "SoulDrive 本地证据链", 90),
            }
        )
    if len(slides) < 2:
        raise ValueError("presentation plan requires at least two slides")
    if slides[0]["kind"] != "cover":
        slides[0]["kind"] = "cover"
    return {
        "schema_version": "souldrive_presentation_plan_v1",
        "generated_at_label": _compact(plan.get("generated_at_label") or time.strftime("%Y-%m-%d %H:%M"), 40),
        "title": _compact(plan.get("title") or inputs.get("title") or "专题汇报", 120),
        "slides": slides,
        "style": _presentation_style(plan.get("style") or _presentation_style_for_skill(inputs.get("style_skill"))),
        "style_skill": dict(inputs.get("style_skill") or {}),
        "presentation_contract": dict(plan.get("presentation_contract") or _presentation_contract_for_skill(inputs.get("style_skill") or {})),
        "skill_application": dict(
            plan.get("skill_application")
            or presentation_skill_application(inputs.get("style_skill"))
        ),
    }


def validate_generated_presentation_evidence(
    plan: dict[str, Any], records: list[dict[str, Any]], inputs: dict[str, Any],
) -> None:
    """Bound model selection to whole source sentences, not lexical entailment.

    ponytail: paraphrases fail closed to extraction until an independently
    evaluated entailment route exists; filename coverage is not fact support.
    """
    def normalized(value: Any) -> str:
        return " ".join(str(value or "").split()).strip("。！？!?；;")

    selected = set(inputs["source_filenames"])
    source_units: dict[str, set[str]] = {source: set() for source in selected}
    for record in records:
        source = str(record.get("source_filename") or "")
        if source in source_units:
            source_units[source].update(
                normalized(unit)
                for unit in re.split(r"(?<=[。！？!?；;])|\n", str(record.get("content") or ""))
                if normalized(unit)
            )
    editorial = {
        "", "专题汇报", "资料摘录", "资料来源", "来源", "执行摘要", "文档要点",
        "多文档综合分析汇报", "SoulDrive 本地证据链",
        normalized(inputs.get("title")), normalized(_default_title(inputs["source_filenames"])),
    }
    editorial.update(normalized(Path(source).stem) for source in selected)
    if normalized(plan.get("title")) not in editorial:
        raise ValueError("PPT 标题缺少原文绑定")
    for slide in plan.get("slides") or []:
        sources = list(slide.get("sources") or [])
        if any(source not in selected for source in sources):
            raise ValueError("PPT 来源超出所选范围")
        units = set().union(*(source_units[source] for source in sources)) if sources else set()
        for field in ("title", "subtitle", "eyebrow", "footer"):
            value = normalized(slide.get(field))
            if value not in editorial and value not in units:
                raise ValueError(f"PPT {field} 缺少原文绑定")
        for point in slide.get("points") or []:
            if not normalized(point) or normalized(point) not in units:
                raise ValueError("PPT 正文缺少所列来源的完整原文绑定")


def _require_generated_outline_evidence(inputs: dict[str, Any], result: dict[str, Any]) -> None:
    if inputs.get("baseline_run_id"):
        # The existing ledger guard independently requires its extractive plan.
        return
    outline = result.get("outline") or {}
    if outline.get("outline_source") in {"local_ollama", "custom_generator"}:
        try:
            validate_generated_presentation_evidence(
                outline, list((result.get("evidence_snapshot") or {}).get("records") or []), inputs,
            )
        except ValueError as exc:
            raise ValueError("自动大纲未通过原文绑定校验，请重新生成并审核；不能继续渲染旧大纲") from exc


def presentation_evidence_metrics(
    plan: dict[str, Any],
    source_count: int,
    records: list[dict[str, Any]],
    selected_document_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Expose source attribution coverage without claiming semantic fact checking."""
    slides = list(plan.get("slides") or [])
    evidence_slides = [slide for slide in slides if slide.get("kind") not in {"cover", "sources"}]
    attributed = [slide for slide in evidence_slides if slide.get("sources")]
    selected_labels = {
        Path(str(record.get("source_filename") or "")).name
        for record in records
        if Path(str(record.get("source_filename") or "")).name
    }
    plan_labels = [
        Path(str(value or "")).name
        for value in plan.get("source_filenames") or []
        if Path(str(value or "")).name
    ]
    if plan_labels:
        selected_labels.update(plan_labels)
    cited_values = [
        str(source or "").strip()
        for slide in evidence_slides
        for source in slide.get("sources") or []
        if str(source or "").strip()
    ]
    cited_labels = {Path(source).name for source in cited_values}
    selected_label_keys = {label.casefold() for label in selected_labels}
    cited_label_keys = {label.casefold() for label in cited_labels}
    cited_label_hits = selected_label_keys & cited_label_keys

    selected_ids = [
        str(value or "").strip()
        for value in (selected_document_ids if selected_document_ids is not None else plan.get("source_document_ids") or [])
        if str(value or "").strip()
    ]
    selected_identity_count = len(dict.fromkeys(selected_ids)) if selected_ids else len(selected_labels)
    cited_identity_indexes: set[int] = set()
    if selected_ids:
        labels_by_index = list(plan_labels)
        for index, document_id in enumerate(selected_ids):
            if index >= len(labels_by_index):
                matching_record = next(
                    (
                        record for record in records
                        if _document_id_matches_value(document_id, {str(record.get("document_id") or "")})
                    ),
                    None,
                )
                labels_by_index.append(Path(str((matching_record or {}).get("source_filename") or "")).name)
        label_to_indexes: dict[str, list[int]] = {}
        for index, label in enumerate(labels_by_index[:len(selected_ids)]):
            label_to_indexes.setdefault(label.casefold(), []).append(index)
        for cited_value in cited_values:
            exact_indexes = [
                index
                for index, document_id in enumerate(selected_ids)
                if _document_id_matches_value(cited_value, {document_id})
            ]
            if exact_indexes:
                cited_identity_indexes.update(exact_indexes)
                continue
            cited_label = Path(cited_value).name.casefold()
            matching_indexes = label_to_indexes.get(cited_label) or []
            if len(matching_indexes) == 1:
                cited_identity_indexes.add(matching_indexes[0])
        cited_identity_count = len(cited_identity_indexes)
        has_duplicate_labels = any(
            len(indexes) > 1 for indexes in label_to_indexes.values()
        )
        if has_duplicate_labels and cited_identity_count < selected_identity_count:
            identity_status = "ambiguous_display_labels"
        else:
            identity_status = "exact_object_ids"
    else:
        cited_identity_count = len(cited_label_hits)
        identity_status = "display_name_only"

    return {
        "evidence_slide_count": len(evidence_slides),
        "attributed_slide_count": len(attributed),
        "evidence_slide_coverage": round(len(attributed) / max(1, len(evidence_slides)), 4),
        "selected_source_count": source_count,
        "selected_source_label_count": len(selected_labels),
        "cited_source_count": len(cited_label_hits),
        "selected_label_coverage": round(len(cited_label_hits) / max(1, len(selected_labels)), 4),
        "selected_identity_count": selected_identity_count,
        "cited_identity_count": cited_identity_count,
        "selected_source_coverage": round(cited_identity_count / max(1, selected_identity_count), 4),
        "source_identity_coverage_status": identity_status,
        "evidence_bound_point_count": sum(len(slide.get("points") or []) for slide in attributed),
    }


def _presentation_contract_for_skill(skill: dict[str, Any] | None) -> dict[str, Any]:
    selected = dict(skill or {})
    contract = selected.get("presentation_contract")
    if isinstance(contract, dict) and contract:
        return contract
    if not selected:
        return {"schema_version": "souldrive_presentation_contract_v1", "adaptation": "system_default", "point_limit": 5}
    text = " ".join(
        [str(selected.get("title") or ""), str(selected.get("content") or ""), str(selected.get("applicability") or "")]
    ).casefold()
    source_format = "html" if re.search(r"single\s+html|html\s+file|webgl|网页|网页ppt", text) else "pptx"
    return {
        "schema_version": "souldrive_presentation_contract_v1",
        "source_format": source_format,
        "local_output_format": "pptx",
        "adaptation": "translated_to_pptx" if source_format == "html" else "native_pptx",
        "layout_profile": "editable_evidence",
        "point_limit": 3 if re.search(r"concise|简洁|要点|evidence|citation|引用|来源", text) else 4,
        "source_policy": "every_content_slide",
        "scripts": "ignored",
        "compatibility_notes": (
            ["远端是网页 HTML PPT；WebGL、演讲者视图和观众同步不会带入本地 PPTX。"]
            if source_format == "html" else []
        ),
    }


def _presentation_style(value: Any) -> dict[str, Any]:
    """Keep Skill-driven presentation styling declarative and allow-listed."""
    defaults: dict[str, Any] = {
        "primary": "173F3B",
        "accent": "DAAB4A",
        "background": "F8F7F2",
        "text": "1C2624",
        "surface": "FFFFFF",
        "cover_variant": "dark",
        "content_variant": "evidence_split",
        "font_family": "Microsoft YaHei",
    }
    if not isinstance(value, dict):
        return defaults
    for key in ("primary", "accent", "background", "text", "surface"):
        color = str(value.get(key) or "").strip().lstrip("#").upper()
        if re.fullmatch(r"[0-9A-F]{6}", color):
            defaults[key] = color
    cover_variant = str(value.get("cover_variant") or "").strip().lower()
    if cover_variant in {"dark", "editorial_light"}:
        defaults["cover_variant"] = cover_variant
    content_variant = str(value.get("content_variant") or "").strip().lower()
    if content_variant in {"evidence_split", "split_cards"}:
        defaults["content_variant"] = content_variant
    font_family = str(value.get("font_family") or "").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9 ._-]{0,39}", font_family):
        defaults["font_family"] = font_family
    return defaults


def _presentation_style_for_skill(skill: dict[str, Any] | None) -> dict[str, Any]:
    """Read only the selected Skill's explicit, declarative style contract."""
    selected = dict(skill or {})
    explicit = selected.get("presentation_style")
    if isinstance(explicit, dict) and explicit:
        return _presentation_style(explicit)
    return _presentation_style({})


def verify_pptx(path: Path, expected_slide_count: int) -> dict[str, Any]:
    errors = []
    actual_count = 0
    if not path.exists():
        errors.append("file_missing")
    elif path.stat().st_size < 8_000:
        errors.append("file_too_small")
    else:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "ppt/presentation.xml" not in names:
                    errors.append("presentation_xml_missing")
                actual_count = sum(
                    1
                    for name in names
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                )
        except zipfile.BadZipFile:
            errors.append("invalid_openxml_package")
    if actual_count != expected_slide_count:
        errors.append("slide_count_mismatch")
    return {
        "valid": not errors,
        "output_path": str(path),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "slide_count": actual_count,
        "expected_slide_count": expected_slide_count,
        "errors": errors,
    }


def find_wps_presentation_executable() -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        registry_paths = (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\wpp.exe"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths\wpp.exe"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths\wps.exe"),
        )
        for hive, path in registry_paths:
            try:
                with winreg.OpenKey(hive, path) as key:
                    executable = str(winreg.QueryValueEx(key, None)[0])
                    if Path(executable).exists():
                        return executable
            except OSError:
                continue
    except ImportError:
        pass
    return None


def resolve_wps_render_script() -> Path:
    candidates = [
        app_root() / "scripts" / "render_pptx_wps.ps1",
        PROJECT_ROOT / "scripts" / "render_pptx_wps.ps1",
        Path(getattr(sys, "_MEIPASS", app_root())) / "scripts" / "render_pptx_wps.ps1",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("WPS presentation renderer script is missing")


def presentation_filename(title: str, sources: list[str], task_id: str) -> str:
    base = title or Path(sources[0]).stem or "SoulDrive-presentation"
    safe = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", base).strip(" ._")[:80]
    suffix = hashlib.sha256(str(task_id).encode("utf-8")).hexdigest()[:16]
    return f"{safe or 'SoulDrive-presentation'}-{suffix}.pptx"


def _evidence_record(parent_id: str, content: str, metadata: dict[str, Any]) -> dict[str, Any]:
    source = Path(str(metadata.get("source_filename") or metadata.get("document_title") or "")).name
    section_path = metadata.get("section_path")
    if isinstance(section_path, list):
        section = " > ".join(str(item) for item in section_path if str(item).strip())
    else:
        section = str(
            metadata.get("section")
            or metadata.get("Header 6")
            or metadata.get("Header 5")
            or metadata.get("Header 4")
            or metadata.get("Header 3")
            or metadata.get("Header 2")
            or metadata.get("Header 1")
            or ""
        )
    return {
        "parent_id": str(parent_id),
        "project_id": str(metadata.get("project_id") or ""),
        "source_filename": source,
        "document_id": str(metadata.get("document_id") or ""),
        "document_title": str(metadata.get("document_title") or Path(source).stem),
        "asset_id": str(metadata.get("asset_id") or ""),
        "asset_version_id": str(metadata.get("asset_version_id") or metadata.get("version_id") or ""),
        "catalog_file_ref": str(metadata.get("catalog_file_ref") or ""),
        "section": _compact(section, 200),
        "page_number": _safe_int(metadata.get("page_number") or metadata.get("page"), 0),
        "parent_index": _safe_int(metadata.get("parent_index"), 0),
        "content": str(content or "").strip()[:4200],
    }


def _ranked_sentences(records: list[dict[str, Any]]) -> list[str]:
    candidates = []
    table_points = []
    seen = set()
    for record in records:
        structured = _presentation_table_points(str(record.get("content") or ""))
        if structured is not None:
            table_points.extend(structured)
            continue
        for sentence in SENTENCE_PATTERN.findall(str(record.get("content") or "")):
            normalized = re.sub(r"^#{1,6}\s+", "", " ".join(sentence.split()).strip(" -•"))
            key = re.sub(r"\W+", "", normalized).casefold()
            if len(normalized) < 16 or len(normalized) > 170 or NOISE_PATTERN.match(normalized):
                continue
            if key in seen:
                continue
            seen.add(key)
            terms = [term.casefold() for term in TERM_PATTERN.findall(normalized)]
            score = len(set(terms)) + (2 if any(char.isdigit() for char in normalized) else 0)
            candidates.append((score, normalized))
    candidates.sort(key=lambda item: (-item[0], len(item[1])))
    return list(dict.fromkeys(table_points + [_compact(sentence, 105) for _, sentence in candidates]))


def _presentation_table_points(content: str) -> list[str] | None:
    """Keep Markdown table headers paired with raw cell values; infer no units."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].startswith("|") or not (lines[1].startswith("|") and lines[1].endswith("|")):
        return None

    def cells(line: str) -> list[str]:
        if not (line.startswith("|") and line.endswith("|")):
            raise TaskPublicError("资料表格行不完整，请缩小范围或重新索引后生成")
        return [value.strip().replace(r"\|", "|") for value in re.split(r"(?<!\\)\|", line[1:-1])]

    header = cells(lines[0])
    separator = cells(lines[1])
    if len(header) != len(separator) or not all(re.fullmatch(r":?-{3,}:?", value) for value in separator):
        return None
    points = []
    for line in lines[2:]:
        row = cells(line)
        if len(row) != len(header):
            raise TaskPublicError("资料表格列数不一致，请核对原文后生成")
        point = "；".join(f"{name or '（空表头）'}：{value or '（原文空白）'}" for name, value in zip(header, row))
        if len(point) > 105:
            raise TaskPublicError("资料表格行过长，不能截断数值，请缩小证据范围")
        points.append(point)
    return points


def _common_terms(records: list[dict[str, Any]], limit: int) -> list[str]:
    sources_by_term: dict[str, set[str]] = {}
    counts: Counter[str] = Counter()
    for record in records:
        source = record["source_filename"]
        for raw_term in set(TERM_PATTERN.findall(str(record.get("content") or ""))):
            term = raw_term.casefold()
            if term in STOP_TERMS or len(term) < 2:
                continue
            sources_by_term.setdefault(term, set()).add(source)
            counts[term] += 1
    ranked = [
        term
        for term, _count in counts.most_common()
        if len(sources_by_term.get(term) or ()) >= 2
    ]
    return ranked[:limit]


def _content_slide(title: str, points: list[str], sources: list[str], footer: str) -> dict[str, Any]:
    return {
        "kind": "content",
        "title": _compact(title, 54),
        "points": [_compact(point, 105) for point in points[:5]],
        "sources": sources[:4],
        "footer": _compact(footer, 90),
    }


def _default_title(sources: list[str]) -> str:
    if len(sources) == 1:
        return Path(sources[0]).stem
    return "多文档综合分析汇报"


def _last_json_object(output: str) -> dict[str, Any]:
    for line in reversed(str(output or "").splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _compact(value: Any, limit: int) -> str:
    normalized = " ".join(str(value or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + "…"


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
