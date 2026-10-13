import argparse
import importlib.util
import hashlib
import json
import math
import os
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.runtime_state import get_runtime_state
from core.answer_quality import validate_answer_citations
from core.evaluation import wilson_interval
from core.interaction_context import SUPPORTED_WORKFLOW_IDS
from core.multimodal_workflows import (
    evaluate_workflow_output_contract,
    workflow_retrieval_variants,
)
from core.rag_defaults import DEFAULT_RAG_TOP_K
from core.security_context import WORKSPACE_DATA_KEY_ENV, get_workspace_keys, restore_workspace_keys, set_workspace_keys
from core.workspace import WORKSPACE_MANIFEST, SoulDriveWorkspace
from core.workspace_crypto import unlock_keystore

CITATION_PATTERN = re.compile(r"\[((?:E|V|A|M)\d+)\]")
REFUSAL_MARKERS = (
    "未找到足够可靠",
    "没有足够",
    "无法回答",
    "不能回答",
    "不会生成可能误导",
    "无相关证据",
    "无法依据现有文档",
    "资料中未包含",
    "证据缺口",
    "无法提供或推断",
)
SOURCE_ALIASES = {
    "大模型知识管理系统": ["大模型知识管理系统"],
    "大模型基准测试体系研究报告": ["大模型基准测试体系研究报告"],
    "数据分类分级规则": ["数据分类分级规则", "数据安全技术数据分类分级规则"],
    "生成式人工智能服务安全基本要求": ["生成式人工智能服务安全基本要求"],
    "知识库问答场景中大语言模型私有化研究与应用实践": ["知识库问答场景中大语言模型私有化研究与应用实践"],
    "大模型私有部署与基础应用落地": ["大模型私有部署与基础应用落地"],
    "城市+AI应用场景清单": ["城市+AI应用场景清单", "城市AI应用场景清单"],
    "综合交通运输大模型智能体创新应用典型案例": [
        "综合交通运输大模型智能体创新应用典型案例",
        "综合交通运输大模型智能体创新应用",
    ],
    "人工智能安全治理蓝皮书": ["人工智能安全治理蓝皮书"],
    "数据要素发展报告": ["数据要素发展报告"],
    "基于大模型和RAG的知识库问答系统": ["基于大模型和RAG的知识库问答系统", "MaxKB"],
    "企业级RAG方案": ["企业级RAG方案", "LazyLLM企业级RAG方案", "LazyLLM"],
    "MaxKB": ["MaxKB", "基于大模型和RAG的知识库问答系统"],
    "LazyLLM": ["LazyLLM", "LazyLLM企业级RAG方案", "企业级RAG方案"],
}


def configure_utf8_stdio():
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8")


@dataclass(frozen=True)
class EvalRecord:
    id: str
    question: str
    expected_sources: list[str]
    type: str
    rubric: list[str]
    expected_keywords: list[str]
    should_refuse: bool
    requires_multi_doc: bool
    graph_relevant: bool
    project_id: str | None = None
    workflow_id: str | None = None
    expected_document_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvalResult:
    id: str
    question: str
    expected_sources: list[str]
    answer: str
    evidence: list[dict[str, Any]]
    elapsed_ms: int | None
    retrieval_ms: int | None = None
    generation_ms: int | None = None
    model_name: str | None = None
    project_id: str | None = None
    workflow_id: str | None = None
    dataset_fingerprint: str | None = None
    retrieval_observability: dict[str, Any] | None = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate SoulDrive retrieval and citation quality.")
    parser.add_argument("--dataset", default="eval/enterprise_zh_v0.2.jsonl", help="JSONL evaluation dataset path.")
    parser.add_argument("--workspace", default=None, help="SoulDrive workspace root. Defaults to current runtime workspace.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_RAG_TOP_K, help="Evidence count to retrieve.")
    parser.add_argument("--include-answers", action="store_true", help="Run full RAG answer generation, not retrieval only.")
    parser.add_argument("--chat-model", default=None, help="Temporarily set SOULDRIVE_CHAT_MODEL for this evaluation run.")
    parser.add_argument("--from-results", default=None, help="Read prior JSONL results instead of running retrieval.")
    parser.add_argument("--save-results", default=None, help="Write JSONL retrieval results for later comparison.")
    parser.add_argument(
        "--save-run-manifest",
        default=None,
        help="Write the existing souldrive_run_manifest_v0.1 lineage file for this evaluation run.",
    )
    parser.add_argument("--run-id", default=None, help="Stable identifier to put in --save-run-manifest.")
    parser.add_argument(
        "--baseline-level",
        choices=("B0_CURRENT", "B1_SIMPLE", "B2_STRONG", "CANDIDATE"),
        default="CANDIDATE",
        help="Baseline label recorded in --save-run-manifest.",
    )
    parser.add_argument("--compare-results", nargs=2, metavar=("BASELINE_JSONL", "CANDIDATE_JSONL"), help="Compare two saved result files.")
    parser.add_argument("--baseline-label", default="baseline", help="Label for the first --compare-results file.")
    parser.add_argument("--candidate-label", default="candidate", help="Label for the second --compare-results file.")
    source_reservation_group = parser.add_mutually_exclusive_group()
    source_reservation_group.add_argument(
        "--enable-comparison-source-reservation",
        action="store_true",
        help="Enable the experimental comparison sub-query source reservation candidate.",
    )
    source_reservation_group.add_argument(
        "--disable-comparison-source-reservation",
        action="store_true",
        help="Explicitly disable comparison sub-query source reservation for a matched baseline run.",
    )
    parser.add_argument(
        "--rerank-candidate-limit",
        type=int,
        default=None,
        help="Optional bounded cross-encoder rerank pool for an explicit latency/recall experiment.",
    )
    parser.add_argument(
        "--enable-source-profile-recall",
        action="store_true",
        help="Enable the experimental document-profile recall stage.",
    )
    parser.add_argument("--passphrase", default=None, help="Workspace passphrase for encrypted local indexes.")
    args = parser.parse_args(argv)

    records = load_dataset(Path(args.dataset))
    if not records:
        print("评测样本数: 0")
        print("没有可评测的数据。")
        return 1

    previous_chat_model = os.environ.get("SOULDRIVE_CHAT_MODEL")
    if args.chat_model:
        os.environ["SOULDRIVE_CHAT_MODEL"] = args.chat_model
    try:
        if args.compare_results:
            baseline_results = load_results(Path(args.compare_results[0]), records)
            candidate_results = load_results(Path(args.compare_results[1]), records)
            baseline_report = summarize(records, baseline_results, top_k=args.top_k, mode="答案生成评测")
            candidate_report = summarize(records, candidate_results, top_k=args.top_k, mode="答案生成评测")
            print_comparison_report(
                baseline_report,
                candidate_report,
                baseline_label=args.baseline_label,
                candidate_label=args.candidate_label,
            )
            return 0

        retrieval_options = {}
        workspace = resolve_workspace_arg(args.workspace) if args.from_results else None
        if args.from_results:
            results = load_results(Path(args.from_results), records)
            mode = "答案生成评测" if any(result.answer.strip() for result in results) else "检索评测"
        else:
            workspace = resolve_workspace_arg(args.workspace)
            if not workspace:
                parser.error(
                    "live retrieval requires a SoulDrive workspace. "
                    "Pass --workspace <path>, set SOULDRIVE_WORKSPACE, or unlock the workspace in the app first."
                )
            prepare_workspace_keys(workspace, args.passphrase)
            if workspace_requires_key(workspace) and get_workspace_keys(workspace) is None:
                parser.error(
                    "encrypted workspace requires unlock credentials. "
                    "Pass --passphrase <workspace password> or set SOULDRIVE_WORKSPACE_PASSPHRASE."
                )
            print(f"评测工作区: {workspace}")
            if args.enable_comparison_source_reservation:
                retrieval_options["enable_comparison_source_reservation"] = True
            elif args.disable_comparison_source_reservation:
                retrieval_options["enable_comparison_source_reservation"] = False
            if args.rerank_candidate_limit is not None:
                retrieval_options["rerank_candidate_limit"] = args.rerank_candidate_limit
            if args.enable_source_profile_recall:
                retrieval_options["enable_source_profile_recall"] = True
            if args.include_answers:
                configure_answer_generation_backend()
                results = run_answer_generation(
                    records,
                    workspace=workspace,
                    top_k=args.top_k,
                    **retrieval_options,
                )
                mode = "答案生成评测"
            else:
                results = run_retrieval(
                    records,
                    workspace=workspace,
                    top_k=args.top_k,
                    **retrieval_options,
                )
                mode = "检索评测"

        if args.save_results:
            save_results(Path(args.save_results), results)

        report = summarize(records, results, top_k=args.top_k, mode=mode)
        if args.save_run_manifest:
            run_id = args.run_id or (
                "eval_"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            )
            save_run_manifest(
                Path(args.save_run_manifest),
                dataset_path=Path(args.dataset),
                records=records,
                results=results,
                mode=mode,
                top_k=args.top_k,
                baseline_level=args.baseline_level,
                run_id=run_id,
                retrieval_options=retrieval_options,
                source_results_path=(Path(args.from_results) if args.from_results else None),
                output_results_path=(Path(args.save_results) if args.save_results else None),
                workspace_path=workspace,
                summary=report,
            )
            print(f"评测运行清单: {args.save_run_manifest}")
        print_report(report)
        return 0
    finally:
        if args.chat_model:
            if previous_chat_model is None:
                os.environ.pop("SOULDRIVE_CHAT_MODEL", None)
            else:
                os.environ["SOULDRIVE_CHAT_MODEL"] = previous_chat_model


def load_dataset(path: Path) -> list[EvalRecord]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if (
            payload.get("review_status") == "pending_review"
            or payload.get("schema_version") == "souldrive_business_seed_review_v0.1"
        ):
            raise ValueError(f"line {line_number}: review seeds are not approved as evaluation gold")
        records.append(
            EvalRecord(
                id=str(payload["id"]),
                question=str(payload["question"]),
                expected_sources=[str(item) for item in payload.get("expected_sources", [])],
                type=str(payload["type"]),
                rubric=[str(item) for item in payload.get("rubric", [])],
                expected_keywords=[str(item) for item in payload.get("expected_keywords", [])],
                should_refuse=bool(payload.get("should_refuse", False)),
                requires_multi_doc=bool(payload.get("requires_multi_doc", False)),
                graph_relevant=bool(payload.get("graph_relevant", False)),
                project_id=_optional_str(payload.get("project_id")),
                workflow_id=_parse_workflow_id(
                    payload.get("workflow_id"),
                    line_number=line_number,
                ),
                expected_document_ids=[
                    str(item)
                    for item in (payload.get("doc_ids") or payload.get("expected_document_ids") or [])
                ],
            )
        )
    return records


def evaluation_dataset_fingerprint(records: list[EvalRecord]) -> str:
    """Return a stable identity for every field that can change scoring."""
    canonical_records = []
    for record in sorted(records, key=lambda item: item.id):
        canonical_records.append(
            {
                "id": record.id,
                "question": record.question,
                "expected_sources": list(record.expected_sources),
                "type": record.type,
                "rubric": list(record.rubric),
                "expected_keywords": list(record.expected_keywords),
                "should_refuse": record.should_refuse,
                "requires_multi_doc": record.requires_multi_doc,
                "graph_relevant": record.graph_relevant,
                "project_id": record.project_id,
                "workflow_id": record.workflow_id,
                "expected_document_ids": list(record.expected_document_ids),
            }
        )
    canonical = json.dumps(
        {
            "schema_version": "souldrive_eval_dataset_fingerprint_v1",
            "records": canonical_records,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _workspace_index_snapshot(
    workspace_path: str | Path | None,
    *,
    sha256_file,
) -> dict[str, Any]:
    """Capture content hashes for the local derived indexes used by RAG.

    This is a lineage hint, not an atomic database snapshot. The explicit
    allow-list keeps manifests small and avoids recursively hashing user
    documents or model directories.
    """
    relative_paths = (
        Path("index/keyword_index.sqlite"),
        Path("index/parent_docs.sqlite"),
        Path("index/knowledge_graph.sqlite"),
        Path("index/secure_graph.sqlite"),
        Path("index/secure_vectors.sqlite"),
        Path("data/documents/manifest.sqlite"),
    )
    expected_paths = [path.as_posix() for path in relative_paths]
    if not workspace_path:
        return {
            "status": "unbound",
            "artifacts": [],
            "missing_paths": expected_paths,
            "artifact_fingerprint": None,
        }

    root = Path(workspace_path).resolve()
    if not root.is_dir():
        return {
            "status": "missing_workspace",
            "artifacts": [],
            "missing_paths": expected_paths,
            "artifact_fingerprint": None,
        }

    artifacts: list[dict[str, Any]] = []
    missing_paths: list[str] = []
    for relative_path in relative_paths:
        artifact_path = root / relative_path
        if not artifact_path.is_file():
            missing_paths.append(relative_path.as_posix())
            continue
        artifacts.append(
            {
                "path": relative_path.as_posix(),
                "sha256": sha256_file(artifact_path),
                "bytes": artifact_path.stat().st_size,
            }
        )

    fingerprint = None
    if artifacts:
        canonical = json.dumps(
            artifacts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return {
        "status": "captured" if artifacts else "empty",
        "artifacts": artifacts,
        "missing_paths": missing_paths,
        "artifact_fingerprint": fingerprint,
    }


def save_run_manifest(
    path: Path,
    *,
    dataset_path: Path,
    records: list[EvalRecord],
    results: list[EvalResult],
    mode: str,
    top_k: int,
    baseline_level: str,
    run_id: str,
    retrieval_options: dict[str, Any] | None = None,
    source_results_path: Path | None = None,
    output_results_path: Path | None = None,
    workspace_path: str | Path | None = None,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the existing v0.1 run-manifest contract for a RAG eval run."""
    allowed_levels = {"B0_CURRENT", "B1_SIMPLE", "B2_STRONG", "CANDIDATE"}
    if baseline_level not in allowed_levels:
        raise ValueError(f"未知评测级别: {baseline_level}")

    from scripts.capture_b0_baseline import (
        gpu_summary,
        package_versions,
        repository_snapshot,
        sha256_file,
    )

    path = Path(path)
    dataset_path = Path(dataset_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    expected_fingerprint = evaluation_dataset_fingerprint(records)
    observed_fingerprints = {
        result.dataset_fingerprint
        for result in results
        if result.dataset_fingerprint
    }
    if observed_fingerprints == {expected_fingerprint} and all(
        result.dataset_fingerprint for result in results
    ):
        fingerprint_status = "verified"
    elif not observed_fingerprints:
        fingerprint_status = "legacy_unbound"
    else:
        fingerprint_status = "mismatch"

    def file_record(file_path: Path, provenance: str) -> dict[str, Any]:
        resolved = file_path.resolve()
        try:
            display_path = resolved.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            display_path = str(resolved)
        return {
            "path": display_path,
            "sha256": sha256_file(resolved),
            "bytes": resolved.stat().st_size,
            "provenance": provenance,
        }

    try:
        repository = repository_snapshot(excluded_root=path.parent)
    except Exception as exc:
        repository = {
            "head": "unknown",
            "branch": "unknown",
            "tracked_diff_sha256": "unknown",
            "untracked_files": [],
            "workspace_state_sha256": "unknown",
        }
        repository_error = str(exc)
    else:
        repository_error = None

    model_names = sorted(
        {
            str(result.model_name).strip()
            for result in results
            if str(result.model_name or "").strip()
        }
    )
    workspace_index_snapshot = _workspace_index_snapshot(
        workspace_path,
        sha256_file=sha256_file,
    )
    outputs = {"manifest": path.name}
    if output_results_path is not None:
        outputs["results"] = str(Path(output_results_path))
    if source_results_path is not None:
        outputs["source_results"] = str(Path(source_results_path))
    limitations = [
        "数据集指纹只约束评测标签，不证明索引、embedding、模型权重、硬件或外部服务完全一致。",
        "输出摘要只反映本次评测脚本记录的指标，不等于答案事实正确率或用户满意度。",
        "工作区索引清单是允许列表文件的内容哈希，不是并发写入下的原子数据库快照。",
    ]
    if workspace_index_snapshot["status"] != "captured":
        limitations.append("本次运行未捕获完整工作区索引文件清单，索引 lineage 仍未绑定。")
    if fingerprint_status != "verified":
        limitations.append("结果未全部绑定当前数据集指纹，不能作为严格版本对比证据。")
    if repository_error:
        limitations.append(f"无法读取 Git 仓库状态: {repository_error}")

    manifest = {
        "schema_version": "souldrive_run_manifest_v0.1",
        "run_id": str(run_id),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline_level": baseline_level,
        "status": "complete",
        "repository": repository,
        "environment": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "conda_environment": "souldrive",
            "packages": package_versions(),
            "gpu": gpu_summary(),
        },
        "inputs": [file_record(dataset_path, "frozen evaluation dataset")],
        "components": {
            "evaluation_script": "scripts/eval_rag.py",
            "mode": mode,
            "top_k": top_k,
            "sample_count": len(records),
            "dataset_fingerprint": expected_fingerprint,
            "dataset_fingerprint_status": fingerprint_status,
            "retrieval_options": dict(retrieval_options or {}),
            "model_names": model_names,
            "workspace_index_snapshot": workspace_index_snapshot,
            "summary": {
                key: value
                for key, value in (summary or {}).items()
                if key != "rows"
            },
        },
        "outputs": outputs,
        "limitations": limitations,
    }
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def load_results(path: Path, records: list[EvalRecord]) -> list[EvalResult]:
    by_id: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            payload = json.loads(line)
            result_id = str(payload.get("id") or "").strip()
            if not result_id:
                raise ValueError(f"评测结果第 {line_number} 行缺少 id，无法与数据集对齐")
            if result_id in by_id:
                raise ValueError(f"评测结果包含重复 id: {result_id}")
            by_id[result_id] = payload

    expected_ids = {record.id for record in records}
    result_ids = set(by_id)
    missing_ids = sorted(expected_ids - result_ids)
    unexpected_ids = sorted(result_ids - expected_ids)
    if missing_ids or unexpected_ids:
        details = []
        if missing_ids:
            details.append(f"缺少 {missing_ids[:5]}")
        if unexpected_ids:
            details.append(f"多出 {unexpected_ids[:5]}")
        raise ValueError(
            f"评测结果与数据集 ID 不匹配（{'；'.join(details)}）。"
            "请使用同一数据集生成和读取结果，禁止跨数据集比较。"
        )

    expected_dataset_fingerprint = evaluation_dataset_fingerprint(records)
    result_fingerprints = []
    results = []
    for record in records:
        payload = by_id.get(record.id, {})
        result_dataset_fingerprint = _optional_str(payload.get("dataset_fingerprint"))
        if result_dataset_fingerprint is not None:
            result_fingerprints.append(result_dataset_fingerprint)
            if result_dataset_fingerprint != expected_dataset_fingerprint:
                raise ValueError(
                    f"评测结果 {record.id} 的 dataset_fingerprint 与当前数据集不一致；"
                    "禁止用新标签重新解释旧结果"
                )
        result_question = str(payload.get("question") or "").strip()
        if result_question and result_question != record.question:
            raise ValueError(f"评测结果 {record.id} 的 question 与数据集不一致")
        if "expected_sources" in payload:
            result_sources = [str(item) for item in payload.get("expected_sources") or []]
            if result_sources != record.expected_sources:
                raise ValueError(f"评测结果 {record.id} 的 expected_sources 与数据集不一致")
        if record.project_id is not None:
            result_project_id = _optional_str(payload.get("project_id"))
            if result_project_id != record.project_id:
                raise ValueError(
                    f"评测结果 {record.id} 的 project_id 与数据集不一致或缺失；"
                    "项目隔离评测禁止使用未带作用域的旧结果"
                )
        elif "project_id" in payload and _optional_str(payload.get("project_id")):
            raise ValueError(
                f"评测结果 {record.id} 带有 project_id，但数据集未指定作用域"
            )
        result_workflow_id = _parse_workflow_id(payload.get("workflow_id"))
        if record.workflow_id is not None:
            if result_workflow_id != record.workflow_id:
                raise ValueError(
                    f"评测结果 {record.id} 的 workflow_id 与数据集不一致或缺失；"
                    "研究窗口评测禁止使用未带角色作用域的旧结果"
                )
        elif result_workflow_id is not None:
            raise ValueError(
                f"评测结果 {record.id} 带有 workflow_id，但数据集未指定研究窗口"
            )
        if "evidence" in payload and not isinstance(payload.get("evidence"), list):
            raise ValueError(f"评测结果 {record.id} 的 evidence 不是列表")
        if (
            payload.get("retrieval_observability") is not None
            and not isinstance(payload.get("retrieval_observability"), dict)
        ):
            raise ValueError(f"评测结果 {record.id} 的 retrieval_observability 不是对象")
        results.append(
            EvalResult(
                id=record.id,
                question=record.question,
                expected_sources=record.expected_sources,
                answer=str(payload.get("answer", "")),
                evidence=list(payload.get("evidence", [])),
                elapsed_ms=_optional_int(payload.get("elapsed_ms")),
                retrieval_ms=_optional_int(payload.get("retrieval_ms")),
                generation_ms=_optional_int(payload.get("generation_ms")),
                model_name=_optional_str(payload.get("model_name")),
                project_id=_optional_str(payload.get("project_id")),
                workflow_id=result_workflow_id,
                dataset_fingerprint=result_dataset_fingerprint,
                retrieval_observability=(
                    dict(payload.get("retrieval_observability") or {})
                    if isinstance(payload.get("retrieval_observability"), dict)
                    else None
                ),
            )
        )
    if result_fingerprints and len(result_fingerprints) != len(records):
        raise ValueError(
            "评测结果只部分绑定 dataset_fingerprint；"
            "禁止把带版本和未带版本的结果混合评分"
        )
    if len(set(result_fingerprints)) > 1:
        raise ValueError("评测结果包含多个 dataset_fingerprint，禁止混合数据集版本")
    return results


def run_retrieval(
    records: list[EvalRecord],
    workspace: str | None,
    top_k: int,
    enable_comparison_source_reservation: bool = False,
    enable_source_profile_recall: bool = False,
    rerank_candidate_limit: int | None = None,
) -> list[EvalResult]:
    from core.knowledge_base import LocalKnowledgeBase

    if not workspace:
        raise ValueError("workspace is required for live retrieval")
    kb = LocalKnowledgeBase(
        workspace_path=workspace,
        enable_usage_ledger=False,
        enable_comparison_source_reservation=enable_comparison_source_reservation,
        enable_source_profile_recall=enable_source_profile_recall,
        rerank_candidate_limit=rerank_candidate_limit,
    )
    graph_db = None
    results = []
    dataset_fingerprint = evaluation_dataset_fingerprint(records)
    try:
        if any(record.graph_relevant for record in records):
            graph_db = _open_retrieval_graph_store(workspace)
        for index, record in enumerate(records, start=1):
            if index == 1 or index % 10 == 0 or index == len(records):
                print(f"[{index}/{len(records)}] 检索评测: {record.id}", flush=True)
            started_at = time.time()
            retrieval_kwargs = {
                "top_k": top_k,
                # An unbound evaluation case is workspace-global, never an
                # implicit all-project corpus read.
                "global_only": not bool(record.project_id),
            }
            if record.project_id:
                retrieval_kwargs["project_id"] = record.project_id
            workflow_variants = workflow_retrieval_variants(
                record.workflow_id,
                record.question,
            )
            if workflow_variants:
                retrieval_kwargs["extra_query_variants"] = workflow_variants
            if record.graph_relevant:
                retrieval_kwargs["graph_db"] = graph_db
            retrieval = kb.search_with_evidence(record.question, **retrieval_kwargs)
            elapsed_ms = int((time.time() - started_at) * 1000)
            results.append(
                EvalResult(
                    id=record.id,
                    question=record.question,
                    expected_sources=record.expected_sources,
                    answer="",
                    evidence=list(retrieval.get("evidence", [])),
                    elapsed_ms=elapsed_ms,
                    project_id=record.project_id,
                    workflow_id=record.workflow_id,
                    dataset_fingerprint=dataset_fingerprint,
                    retrieval_observability=(
                        dict(retrieval.get("observability") or {})
                        if isinstance(retrieval.get("observability"), dict)
                        else None
                    ),
                )
            )
    finally:
        if graph_db is not None:
            close_graph = getattr(graph_db, "close", None)
            if callable(close_graph):
                close_graph()
        kb.close()
    return results


def _open_retrieval_graph_store(workspace: str):
    """Open the same graph backend used by answer generation for graph evals."""
    workspace_obj = SoulDriveWorkspace(workspace).ensure()
    keys = get_workspace_keys(workspace_obj.root_path)
    if keys is not None:
        from core.secure_graph_store import SecureGraphStore

        return SecureGraphStore(workspace_obj.secure_graph_store_path, keys)

    from core.graph_db import LocalGraphDB

    return LocalGraphDB(db_path=workspace_obj.graph_db_path)


def loaded_chat_model_name(llm: Any) -> str | None:
    """Read the loaded client's identity, never a requested runtime default."""
    for attribute in ("model", "model_name"):
        loaded_name = getattr(llm, attribute, None)
        if isinstance(loaded_name, str) and loaded_name.strip():
            return loaded_name.strip()
    loaded_path = getattr(llm, "model_path", None)
    if isinstance(loaded_path, (str, Path)) and str(loaded_path).strip():
        return Path(loaded_path).name
    return None


def run_answer_generation(
    records: list[EvalRecord],
    workspace: str | None,
    top_k: int,
    enable_comparison_source_reservation: bool = False,
    enable_source_profile_recall: bool = False,
    rerank_candidate_limit: int | None = None,
) -> list[EvalResult]:
    from core.audit_log import AuditLogger
    from core.knowledge_base import LocalKnowledgeBase
    from core.rag_engine import RAGEngine

    if not workspace:
        raise ValueError("workspace is required for answer generation")
    workspace_obj = SoulDriveWorkspace(workspace).ensure()
    kb = LocalKnowledgeBase(
        workspace_path=workspace,
        enable_usage_ledger=False,
        enable_comparison_source_reservation=enable_comparison_source_reservation,
        enable_source_profile_recall=enable_source_profile_recall,
        rerank_candidate_limit=rerank_candidate_limit,
    )
    rag = None
    results = []
    dataset_fingerprint = evaluation_dataset_fingerprint(records)
    try:
        rag = RAGEngine(
            kb=kb,
            graph_db_path=workspace_obj.graph_db_path,
            workspace_path=workspace_obj.root_path,
            audit_logger=AuditLogger.for_workspace(workspace_obj),
        )
        for index, record in enumerate(records, start=1):
            print(f"[{index}/{len(records)}] 生成答案: {record.id}", flush=True)
            started_at = time.time()
            retrieval_observability: dict[str, Any] = {}
            answer, evidence = generate_answer_with_fallback(
                rag,
                record.question,
                project_id=record.project_id,
                workflow_id=record.workflow_id,
                top_k=top_k,
                on_retrieval_completed=retrieval_observability.update,
            )
            elapsed_ms = int((time.time() - started_at) * 1000)
            model_name = loaded_chat_model_name(getattr(rag, "llm", None))
            results.append(
                EvalResult(
                    id=record.id,
                    question=record.question,
                    expected_sources=record.expected_sources,
                    answer=answer,
                    evidence=evidence,
                    elapsed_ms=elapsed_ms,
                    model_name=model_name,
                    project_id=record.project_id,
                    workflow_id=record.workflow_id,
                    dataset_fingerprint=dataset_fingerprint,
                    retrieval_observability=retrieval_observability or None,
                )
            )
    finally:
        if rag is not None:
            rag.close()
        kb.close()
    return results


def configure_answer_generation_backend() -> None:
    if str(os.environ.get("SOULDRIVE_CHAT_PROVIDER") or "").strip():
        return
    if importlib.util.find_spec("llama_cpp") is not None:
        return
    os.environ["SOULDRIVE_CHAT_PROVIDER"] = "ollama"
    if not str(os.environ.get("SOULDRIVE_OLLAMA_MODEL") or "").strip():
        os.environ["SOULDRIVE_OLLAMA_MODEL"] = "qwen3.5:9b"
    if not str(os.environ.get("SOULDRIVE_OLLAMA_URL") or "").strip():
        os.environ["SOULDRIVE_OLLAMA_URL"] = "http://127.0.0.1:11434"


def generate_answer_with_fallback(
    rag,
    query: str,
    *,
    project_id: str | None,
    top_k: int,
    workflow_id: str | None = None,
    on_retrieval_completed=None,
) -> tuple[str, list[dict[str, Any]]]:
    from core.rag_engine import (
        _ensure_workflow_output_sections,
        ensure_answer_section_label,
        evidence_fallback_answer,
    )

    interaction_context = None
    if project_id or workflow_id:
        interaction_context = {}
        if project_id:
            interaction_context["active_context"] = {"project_id": project_id}
        if workflow_id:
            interaction_context["workflow_id"] = workflow_id
    try:
        stream_text = "".join(
            rag.generate_response_stream(
                query,
                top_k=top_k,
                interaction_context=interaction_context,
                on_retrieval_completed=on_retrieval_completed,
            )
        )
        answer, evidence = split_answer_and_evidence(stream_text)
        # A refusal is a completed safety result even when it intentionally
        # carries no evidence block. Never replace it with an un-gated
        # extractive fallback, which could re-open sensitive or out-of-scope
        # content after the main RAG path has refused.
        if answer.strip() and answer_refuses(answer):
            if not evidence and callable(on_retrieval_completed):
                on_retrieval_completed({
                    "schema_version": "agentic_retrieval_trace_v1",
                    "status": "refused",
                    "agentic_decision": "refuse",
                    "stop_reason": "completed_refusal",
                })
            return answer, evidence
        if answer.strip() and evidence:
            return answer, evidence
        raise ValueError("generated answer missing usable evidence")
    except Exception:
        if callable(on_retrieval_completed):
            on_retrieval_completed({
                "schema_version": "agentic_retrieval_trace_v1",
                "status": "fallback",
                "agentic_decision": "fallback",
                "stop_reason": "generation_or_stream_failed",
            })
        retrieval_kwargs = {
            "query": query,
            "graph_db": rag.graph_db,
            "top_k": top_k,
            "project_id": project_id,
            "global_only": not bool(project_id),
        }
        workflow_variants = workflow_retrieval_variants(workflow_id, query)
        if workflow_variants:
            retrieval_kwargs["extra_query_variants"] = workflow_variants
        retrieved = rag.kb.search_with_evidence(
            **retrieval_kwargs,
        )
        evidence = list(retrieved.get("evidence") or [])
        answer = ensure_answer_section_label(
            evidence_fallback_answer(query, evidence, prefer_primary=True),
            evidence,
        )
        answer = _ensure_workflow_output_sections(workflow_id, answer)
        return answer, evidence


EVIDENCE_BLOCK_PATTERN = re.compile(r"```souldrive-evidence\s*(.*?)\s*```", re.DOTALL)
AUXILIARY_BLOCK_PATTERN = re.compile(r"```souldrive-(?:evidence|mindmap)\s*.*?\s*```", re.DOTALL)


def split_answer_and_evidence(stream_text: str) -> tuple[str, list[dict[str, Any]]]:
    evidence: list[dict[str, Any]] = []
    matches = list(EVIDENCE_BLOCK_PATTERN.finditer(stream_text or ""))
    if matches:
        try:
            parsed = json.loads(matches[-1].group(1).strip())
            if isinstance(parsed, list):
                evidence = [item for item in parsed if isinstance(item, dict)]
        except json.JSONDecodeError:
            evidence = []
    answer = AUXILIARY_BLOCK_PATTERN.sub("", stream_text or "").strip()
    return answer, evidence


def resolve_workspace_arg(workspace: str | None) -> str | None:
    runtime_workspace = str(get_runtime_state().get("workspace_path") or "")
    candidates = [
        workspace,
        os.environ.get("SOULDRIVE_WORKSPACE"),
        find_unlocked_drive_workspace(),
        str(PROJECT_ROOT / "souldrive_db"),
        runtime_workspace,
    ]
    for candidate in candidates:
        if candidate and is_workspace_path(candidate):
            return str(Path(candidate))
    return None


def is_workspace_path(path: str) -> bool:
    if not path:
        return False
    root = Path(path)
    return (root / "config" / WORKSPACE_MANIFEST).exists()


def prepare_workspace_keys(workspace_path: str, passphrase: str | None) -> bool:
    if restore_workspace_keys(workspace_path, os.environ.get(WORKSPACE_DATA_KEY_ENV)):
        return True

    passphrase = passphrase or os.environ.get("SOULDRIVE_WORKSPACE_PASSPHRASE")
    if not passphrase:
        return False

    workspace = SoulDriveWorkspace(workspace_path).ensure()
    keys = unlock_keystore(workspace, passphrase)
    set_workspace_keys(workspace.root_path, keys)
    return True


def workspace_requires_key(workspace_path: str) -> bool:
    return (Path(workspace_path) / "index" / "secure_vectors.sqlite").exists()


def find_unlocked_drive_workspace() -> str | None:
    for drive in iter_filesystem_drives():
        workspace = Path(drive.mountpoint) / "SoulDrive"
        if not is_workspace_path(str(workspace)):
            continue

        state_path = workspace / "runtime" / "runtime_state.json"
        if not state_path.exists():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if state.get("locked") is False:
            state_workspace = str(state.get("workspace_path") or workspace)
            if is_workspace_path(state_workspace):
                return str(Path(state_workspace))
    return None


def iter_filesystem_drives():
    try:
        import psutil

        return psutil.disk_partitions(all=False)
    except Exception:
        if os.name != "nt":
            return []
        drives = []
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            root = f"{letter}:\\"
            if Path(root).exists():
                drives.append(type("Drive", (), {"mountpoint": root})())
        return drives


def save_results(path: Path, results: list[EvalResult]):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for result in results:
        lines.append(
            json.dumps(
                {
                    "id": result.id,
                    "question": result.question,
                    "expected_sources": result.expected_sources,
                    "answer": result.answer,
                    "evidence": result.evidence,
                    "elapsed_ms": result.elapsed_ms,
                    "retrieval_ms": result.retrieval_ms,
                    "generation_ms": result.generation_ms,
                    "model_name": result.model_name,
                    "project_id": result.project_id,
                    "workflow_id": result.workflow_id,
                    "dataset_fingerprint": result.dataset_fingerprint,
                    "retrieval_observability": result.retrieval_observability,
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def summarize(records: list[EvalRecord], results: list[EvalResult], top_k: int, mode: str | None = None) -> dict[str, Any]:
    result_by_id = {result.id: result for result in results}
    rows = []
    for record in records:
        result = result_by_id[record.id]
        rows.append(score_record(record, result, top_k=top_k))

    resolved_mode = mode or (
        "答案生成评测"
        if any(result.answer.strip() for result in results)
        else "检索评测"
    )
    answer_evaluation_enabled = resolved_mode == "答案生成评测"
    positive_rows = [row for row in rows if row["expected_sources"]]
    answer_rows = [row for row in rows if row["has_answer"]]
    answer_evaluation_rows = rows if answer_evaluation_enabled else []
    refusal_rows = [row for row in answer_evaluation_rows if row["should_refuse"]]
    keyword_rows = [row for row in answer_evaluation_rows if row["keyword_sample"]]
    answerable_rows = [row for row in answer_evaluation_rows if not row["should_refuse"]]
    answer_source_rows = [
        row
        for row in answer_evaluation_rows
        if row["expected_sources"] and not row["should_refuse"]
    ]
    context_precision_rows = [row for row in rows if row["context_precision"] is not None]
    citation_support_rows = [row for row in rows if row["citation_support_applicable"]]
    scoped_rows = [row for row in rows if row["project_id"]]
    workflow_rows = [row for row in rows if row["workflow_id"]]
    workflow_contract_rows = [
        row for row in workflow_rows if row["workflow_contract_valid"] is not None
    ]
    multi_doc_rows = [row for row in rows if row["requires_multi_doc"]]
    graph_rows = [row for row in rows if row["graph_relevant"]]
    graph_observability_rows = [
        row for row in graph_rows
        if row["graph_route_selected"] is not None
        and row["graph_stage_executed"] is not None
        and row["graph_context_nonempty"] is not None
    ]
    latencies = [row["elapsed_ms"] for row in rows if row["elapsed_ms"] is not None]
    retrieval_latencies = [row["retrieval_ms"] for row in rows if row["retrieval_ms"] is not None]
    generation_latencies = [row["generation_ms"] for row in rows if row["generation_ms"] is not None]
    model_names = sorted({row["model_name"] for row in rows if row["model_name"]})
    answer_sample_count = len(answer_evaluation_rows)
    expected_dataset_fingerprint = evaluation_dataset_fingerprint(records)
    observed_dataset_fingerprints = {
        result.dataset_fingerprint
        for result in results
        if result.dataset_fingerprint
    }
    if not observed_dataset_fingerprints:
        dataset_fingerprint_status = "legacy_unbound"
    elif observed_dataset_fingerprints == {expected_dataset_fingerprint} and all(
        result.dataset_fingerprint for result in results
    ):
        dataset_fingerprint_status = "verified"
    else:
        dataset_fingerprint_status = "mismatch"

    workflow_breakdown = {}
    for workflow_id in sorted({row["workflow_id"] for row in workflow_rows}):
        workflow_group = [row for row in workflow_rows if row["workflow_id"] == workflow_id]
        contract_group = [
            row for row in workflow_group if row["workflow_contract_valid"] is not None
        ]
        workflow_breakdown[workflow_id] = {
            "sample_count": len(workflow_group),
            "answer_sample_count": sum(row["has_answer"] for row in workflow_group),
            "workflow_contract_sample_count": len(contract_group),
            "workflow_output_contract_pass_rate": _ratio(
                sum(bool(row["workflow_contract_valid"]) for row in contract_group),
                len(contract_group),
            ),
            "retrieval_any_hit_rate": _ratio(
                sum(row["source_any_hit"] for row in workflow_group if row["expected_sources"]),
                sum(bool(row["expected_sources"]) for row in workflow_group),
            ),
            "answerable_pass_rate": _ratio(
                sum(row["answerable_pass"] for row in workflow_group if row["has_answer"]),
                sum(bool(row["has_answer"]) for row in workflow_group),
            ),
        }

    retrieval_all_hits = sum(row["source_all_hit"] for row in positive_rows)
    retrieval_any_hits = sum(row["source_any_hit"] for row in positive_rows)
    first_hits = sum(row["first_hit"] for row in positive_rows)
    multi_doc_hits = sum(row["source_all_hit"] for row in multi_doc_rows)
    graph_hits = sum(row["source_any_hit"] for row in graph_rows)
    return {
        "mode": resolved_mode,
        "sample_count": len(rows),
        "answer_sample_count": answer_sample_count,
        "top_k": top_k,
        "dataset_fingerprint": (
            expected_dataset_fingerprint
            if dataset_fingerprint_status == "verified"
            else None
        ),
        "dataset_fingerprint_status": dataset_fingerprint_status,
        "model_name": model_names[0] if len(model_names) == 1 else ("mixed" if len(model_names) > 1 else None),
        "retrieval_all_hit_rate": _ratio(retrieval_all_hits, len(positive_rows)),
        "retrieval_any_hit_rate": _ratio(retrieval_any_hits, len(positive_rows)),
        "first_hit_rate": _ratio(first_hits, len(positive_rows)),
        "mean_reciprocal_rank": _ratio(
            sum(row["reciprocal_rank"] for row in positive_rows),
            len(positive_rows),
        ),
        "average_source_coverage": _ratio(
            sum(row["source_coverage"] for row in positive_rows),
            len(positive_rows),
        ),
        "context_precision_sample_count": len(context_precision_rows),
        "average_context_precision": _ratio(
            sum(row["context_precision"] for row in context_precision_rows),
            len(context_precision_rows),
        ),
        "multi_doc_all_hit_rate": _ratio(multi_doc_hits, len(multi_doc_rows)),
        # Keep the historical field for comparison compatibility. It is a
        # source hit metric, not proof that the graph branch ran.
        "graph_context_hit_rate": _ratio(graph_hits, len(graph_rows)),
        "graph_source_hit_rate": _ratio(graph_hits, len(graph_rows)),
        "graph_observability_sample_count": len(graph_observability_rows),
        "graph_route_selected_rate": _ratio(
            sum(row["graph_route_selected"] for row in graph_observability_rows),
            len(graph_observability_rows),
        ),
        "graph_stage_executed_rate": _ratio(
            sum(row["graph_stage_executed"] for row in graph_observability_rows),
            len(graph_observability_rows),
        ),
        "graph_context_nonempty_rate": _ratio(
            sum(row["graph_context_nonempty"] for row in graph_observability_rows),
            len(graph_observability_rows),
        ),
        "retrieval_confidence_intervals_95": {
            "retrieval_all_hit_rate": wilson_interval(
                retrieval_all_hits,
                len(positive_rows),
            ),
            "retrieval_any_hit_rate": wilson_interval(
                retrieval_any_hits,
                len(positive_rows),
            ),
            "first_hit_rate": wilson_interval(first_hits, len(positive_rows)),
            "multi_doc_all_hit_rate": wilson_interval(
                multi_doc_hits,
                len(multi_doc_rows),
            ),
            "graph_context_hit_rate": wilson_interval(
                graph_hits,
                len(graph_rows),
            ),
        },
        "citation_valid_rate": _ratio(sum(row["citation_valid"] for row in answer_rows), len(answer_rows)),
        "invalid_citation_rate": _ratio(sum(not row["citation_valid"] for row in answer_rows), len(answer_rows)),
        "citation_support_valid_rate": _ratio(
            sum(bool(row["citation_support_valid"]) for row in citation_support_rows),
            len(citation_support_rows),
        ),
        "citation_support_invalid_rate": _ratio(
            sum(not bool(row["citation_support_valid"]) for row in citation_support_rows),
            len(citation_support_rows),
        ),
        "claim_support_checked_count": sum(row["claim_support_checked_count"] for row in rows),
        "claim_support_unsupported_claim_count": sum(
            row["claim_support_unsupported_claim_count"] for row in rows
        ),
        "claim_support_unsupported_rate": _ratio(
            sum(row["claim_support_unsupported_claim_count"] for row in rows),
            sum(row["claim_support_checked_count"] for row in rows),
        ),
        "refusal_accuracy": _ratio(sum(row["refusal_correct"] for row in refusal_rows), len(refusal_rows)),
        "refusal_leak_rate": _ratio(sum(row["refusal_leak"] for row in refusal_rows), len(refusal_rows)),
        "false_refusal_rate": _ratio(sum(row["false_refusal"] for row in answerable_rows), len(answerable_rows)),
        "scoped_sample_count": len(scoped_rows),
        "wrong_project_sample_count": sum(row["wrong_project"] for row in scoped_rows),
        "wrong_project_rate": _ratio(
            sum(row["wrong_project"] for row in scoped_rows),
            len(scoped_rows),
        ),
        "unscoped_sample_count": len(rows) - len(scoped_rows),
        "workflow_sample_count": len(workflow_rows),
        "workflow_contract_sample_count": len(workflow_contract_rows),
        "workflow_output_contract_pass_rate": _ratio(
            sum(bool(row["workflow_contract_valid"]) for row in workflow_contract_rows),
            len(workflow_contract_rows),
        ),
        "workflow_breakdown": workflow_breakdown,
        "answerable_pass_rate": _ratio(sum(row["answerable_pass"] for row in answerable_rows), len(answerable_rows)),
        "answer_expected_source_all_hit_rate": _ratio(
            sum(bool(row["expected_source_citation_all"]) for row in answer_source_rows),
            len(answer_source_rows),
        ),
        "answer_expected_source_coverage": _ratio(
            sum(row["expected_source_citation_coverage"] for row in answer_source_rows),
            len(answer_source_rows),
        ),
        "overall_reliability_rate": _ratio(
            sum(row["reliable_answer"] for row in answer_evaluation_rows),
            len(answer_evaluation_rows),
        ),
        "answer_keyword_coverage": _ratio(sum(row["keyword_coverage"] for row in keyword_rows), len(keyword_rows)),
        "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else None,
        "avg_retrieval_ms": int(sum(retrieval_latencies) / len(retrieval_latencies)) if retrieval_latencies else None,
        "avg_generation_ms": int(sum(generation_latencies) / len(generation_latencies)) if generation_latencies else None,
        "rows": rows,
    }


def score_record(record: EvalRecord, result: EvalResult, top_k: int) -> dict[str, Any]:
    if record.workflow_id != result.workflow_id:
        raise ValueError(
            f"评测样本 {record.id} 的 workflow_id 与结果不一致，禁止跨研究窗口评分"
        )
    evidence = result.evidence[:top_k]
    answer = result.answer or ""
    expected_sources = record.expected_sources
    expected_document_ids = list(record.expected_document_ids or [])
    if expected_document_ids and len(expected_document_ids) != len(expected_sources):
        raise ValueError(f"评测样本 {record.id} 的 doc_ids 数量必须与 expected_sources 一致")
    source_hits = [
        any(
            evidence_matches_expected_source(
                item,
                expected,
                expected_document_ids[index] if expected_document_ids else None,
            )
            for item in evidence
        )
        for index, expected in enumerate(expected_sources)
    ]
    first_evidence = evidence[0] if evidence else {}
    first_hit = bool(expected_sources) and any(
        evidence_matches_expected_source(
            first_evidence,
            expected,
            expected_document_ids[index] if expected_document_ids else None,
        )
        for index, expected in enumerate(expected_sources)
    )
    first_match_rank = first_matching_rank(evidence, expected_sources, expected_document_ids=expected_document_ids)
    source_coverage = (sum(source_hits) / len(expected_sources)) if expected_sources else 0.0
    context_precision = context_precision_score(
        evidence,
        expected_sources,
        expected_document_ids=expected_document_ids,
    )
    citation_valid = citations_are_valid(answer, evidence)
    answer_refused = answer_refuses(answer)
    expected_project_id = str(record.project_id or "").strip()
    wrong_project_evidence = []
    if expected_project_id:
        wrong_project_evidence = [
            item for item in evidence
            if str(item.get("project_id") or "").strip()
            and str(item.get("project_id") or "").strip() != expected_project_id
        ]
    citation_support = citation_support_summary(
        answer,
        evidence,
        citation_valid=citation_valid,
        answer_refused=answer_refused or record.should_refuse,
    )
    refusal_correct = record.should_refuse and answer_refused
    source_requirement = "all" if record.requires_multi_doc else "any"
    source_ok = (not expected_sources) or (
        all(source_hits) if record.requires_multi_doc else any(source_hits)
    )
    cited_ids = set(CITATION_PATTERN.findall(answer))
    cited_source_hits = [
        any(
            str(item.get("id") or "") in cited_ids
            and evidence_matches_expected_source(
                item,
                expected,
                expected_document_ids[index] if expected_document_ids else None,
            )
            for item in evidence
        )
        for index, expected in enumerate(expected_sources)
    ]
    expected_source_citation_requirement = source_requirement
    expected_source_citation_ok = (not expected_sources) or (
        all(cited_source_hits)
        if record.requires_multi_doc
        else any(cited_source_hits)
    )
    expected_source_citation_coverage = (
        sum(cited_source_hits) / len(expected_sources)
        if expected_sources
        else 0.0
    )
    scope_ok = not wrong_project_evidence if expected_project_id else None
    keyword_coverage = keyword_coverage_ratio(answer, record.expected_keywords)
    keyword_pass = (not record.expected_keywords) or keyword_coverage >= 0.5
    workflow_output_contract = None
    workflow_contract_valid = None
    if record.workflow_id and answer.strip():
        workflow_output_contract = evaluate_workflow_output_contract(
            record.workflow_id,
            answer,
        )
        workflow_contract_valid = workflow_output_contract.get("status") == "pass"
    retrieval_observability = (
        result.retrieval_observability
        if isinstance(result.retrieval_observability, dict)
        else {}
    )
    graph_route = (
        retrieval_observability.get("graph_route")
        if isinstance(retrieval_observability.get("graph_route"), dict)
        else {}
    )
    graph_route_selected = (
        bool(graph_route.get("use_graph"))
        if "use_graph" in graph_route
        else None
    )
    graph_stage_executed = (
        bool(retrieval_observability.get("graph_recall_enabled"))
        if "graph_recall_enabled" in retrieval_observability
        else None
    )
    graph_context_count = _optional_int(
        retrieval_observability.get("graph_context_count")
    )
    graph_context_nonempty = (
        graph_context_count > 0
        if graph_context_count is not None
        else None
    )
    answerable_pass = (
        bool(answer.strip())
        and not record.should_refuse
        and not answer_refused
        and citation_support["valid"] is True
        and source_ok
        and expected_source_citation_ok
        and scope_ok is not False
        and keyword_pass
        and workflow_contract_valid is not False
    )
    reliable_answer = refusal_correct if record.should_refuse else answerable_pass
    return {
        "id": record.id,
        "question": record.question,
        "project_id": record.project_id,
        "workflow_id": record.workflow_id,
        "expected_sources": expected_sources,
        "expected_document_ids": expected_document_ids,
        "document_identity_required": bool(expected_document_ids),
        "top_sources": [str(item.get("source_filename") or "未知来源") for item in evidence],
        "top_document_ids": [evidence_document_id(item) for item in evidence],
        "has_answer": bool(answer.strip()),
        "source_all_hit": bool(expected_sources) and all(source_hits),
        "source_any_hit": bool(expected_sources) and any(source_hits),
        "first_hit": first_hit,
        "first_match_rank": first_match_rank,
        "reciprocal_rank": (1.0 / first_match_rank) if first_match_rank else 0.0,
        "source_coverage": source_coverage,
        "context_precision": context_precision,
        "source_requirement": source_requirement,
        "source_requirement_satisfied": source_ok,
        "expected_source_citation_requirement": expected_source_citation_requirement,
        "expected_source_citation_all": bool(expected_sources) and all(cited_source_hits),
        "expected_source_citation_any": bool(expected_sources) and any(cited_source_hits),
        "expected_source_citation_satisfied": expected_source_citation_ok,
        "expected_source_citation_coverage": expected_source_citation_coverage,
        "wrong_project": bool(wrong_project_evidence),
        "wrong_project_evidence_count": len(wrong_project_evidence),
        "scope_ok": scope_ok,
        "workflow_output_contract": workflow_output_contract,
        "workflow_contract_valid": workflow_contract_valid,
        "citation_valid": citation_valid,
        "citation_support_applicable": citation_support["applicable"],
        "citation_support_valid": citation_support["valid"],
        "claim_support_checked_count": citation_support["checked_claim_count"],
        "claim_support_unsupported_claim_count": citation_support["unsupported_claim_count"],
        "citation_support_reason": citation_support["reason"],
        "refusal_correct": refusal_correct,
        "refusal_leak": bool(record.should_refuse and answer.strip() and not answer_refused),
        "false_refusal": bool(not record.should_refuse and answer.strip() and answer_refused),
        "answerable_pass": answerable_pass,
        "reliable_answer": reliable_answer,
        "should_refuse": record.should_refuse,
        "requires_multi_doc": record.requires_multi_doc,
        "graph_relevant": record.graph_relevant,
        "graph_route_selected": graph_route_selected,
        "graph_stage_executed": graph_stage_executed,
        "graph_context_count": graph_context_count,
        "graph_context_nonempty": graph_context_nonempty,
        "keyword_sample": bool(record.expected_keywords and not record.should_refuse),
        "keyword_coverage": keyword_coverage,
        "elapsed_ms": result.elapsed_ms,
        "retrieval_ms": result.retrieval_ms,
        "generation_ms": result.generation_ms,
        "model_name": result.model_name,
    }


def source_matches(source_filename: Any, expected: str) -> bool:
    return _source_matches(source_filename, expected, exact=False)


def source_matches_exact(source_filename: Any, expected: str) -> bool:
    return _source_matches(source_filename, expected, exact=True)


def _source_matches(source_filename: Any, expected: str, *, exact: bool) -> bool:
    source = normalize_source_name(str(source_filename or ""))
    for alias in source_aliases(expected):
        normalized_alias = normalize_source_name(alias)
        if normalized_alias and (normalized_alias == source if exact else normalized_alias in source):
            return True
    return False


def context_precision_score(
    evidence: list[dict[str, Any]],
    expected_sources: list[str],
    *,
    expected_document_ids: list[str] | None = None,
) -> float | None:
    """Measure whether relevant retrieved contexts appear before noise.

    This is a lightweight, label-based analogue of Ragas Context Precision:
    each retrieved item is relevant when its source matches an expected source,
    and the average precision at relevant ranks is reported.  Samples without
    expected sources are not applicable because a refusal question has no
    positive retrieval label.
    """
    if not expected_sources:
        return None
    if not evidence:
        return 0.0

    relevant_count = 0
    precision_sum = 0.0
    expected_document_ids = list(expected_document_ids or [])
    for rank, item in enumerate(evidence, start=1):
        if not any(
            evidence_matches_expected_source(
                item,
                expected,
                expected_document_ids[index] if expected_document_ids else None,
            )
            for index, expected in enumerate(expected_sources)
        ):
            continue
        relevant_count += 1
        precision_sum += relevant_count / rank
    if not relevant_count:
        return 0.0
    return precision_sum / relevant_count


def source_aliases(expected: str) -> list[str]:
    aliases = SOURCE_ALIASES.get(expected, [])
    return [expected, *aliases]


def evidence_document_id(item: dict[str, Any]) -> str:
    for key in ("document_id", "doc_id", "source_doc_id"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        for key in ("document_id", "doc_id", "source_doc_id"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
    return ""


def evidence_matches_expected_source(
    item: dict[str, Any],
    expected_source: str,
    expected_document_id: str | None = None,
) -> bool:
    if expected_document_id:
        evaluation_id = evidence_evaluation_id(item)
        if evaluation_id:
            return evaluation_id == str(expected_document_id).strip()
        # The dataset's SDOC-* value is a corpus label, while runtime
        # document_id is an asset/path identity.  Do not compare those
        # different namespaces; use an exact normalized source/alias match
        # unless the result explicitly carries an evaluation identity.
        return source_matches_exact(item.get("source_filename"), expected_source)
    return source_matches(item.get("source_filename"), expected_source)


def evidence_evaluation_id(item: dict[str, Any]) -> str:
    keys = ("evaluation_doc_id", "corpus_doc_id", "gold_document_id")
    for container in (item, item.get("metadata")):
        if not isinstance(container, dict):
            continue
        for key in keys:
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return ""


def normalize_source_name(value: str) -> str:
    lowered = value.lower()
    for suffix in (".pdf", ".docx", ".md", ".html", ".txt"):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return re.sub(r"[\s_\-—–·,，。\.《》<>“”\"'‘’()（）【】\[\]：:；;]+", "", lowered)


def first_matching_rank(
    evidence: list[dict[str, Any]],
    expected_sources: list[str],
    *,
    expected_document_ids: list[str] | None = None,
) -> int | None:
    if not expected_sources:
        return None
    expected_document_ids = list(expected_document_ids or [])
    for rank, item in enumerate(evidence, start=1):
        if any(
            evidence_matches_expected_source(
                item,
                expected,
                expected_document_ids[index] if expected_document_ids else None,
            )
            for index, expected in enumerate(expected_sources)
        ):
            return rank
    return None


def keyword_coverage_ratio(answer: str, expected_keywords: list[str]) -> float:
    keywords = [keyword for keyword in expected_keywords if keyword]
    if not answer.strip() or not keywords:
        return 0.0
    normalized_answer = answer.lower()
    hits = sum(1 for keyword in keywords if keyword.lower() in normalized_answer)
    return hits / len(keywords)


def citations_are_valid(answer: str, evidence: list[dict[str, Any]]) -> bool:
    cited_ids = set(CITATION_PATTERN.findall(answer or ""))
    if not answer:
        return True
    if not cited_ids:
        return answer_refuses(answer)
    available_ids = {str(item.get("id")) for item in evidence if item.get("id")}
    return cited_ids <= available_ids


def citation_support_summary(
    answer: str,
    evidence: list[dict[str, Any]],
    *,
    citation_valid: bool,
    answer_refused: bool,
) -> dict[str, Any]:
    """Summarize the production claim-support gate for offline evaluation.

    Refusals and empty answers are outside the claim-support denominator.  A
    non-refusal answer still fails when its citation IDs are invalid, even if
    the core validator has no body text to inspect.
    """
    if not answer.strip() or answer_refused:
        return {
            "applicable": False,
            "valid": None,
            "checked_claim_count": 0,
            "unsupported_claim_count": 0,
            "reason": "not_applicable",
        }
    report = validate_answer_citations(answer, evidence, semantic=True)
    semantic = report.get("semantic_support") or {}
    return {
        "applicable": True,
        "valid": bool(citation_valid and report.get("valid")),
        "checked_claim_count": int(semantic.get("checked_claim_count") or 0),
        "unsupported_claim_count": int(semantic.get("unsupported_claim_count") or 0),
        "reason": str(report.get("reason") or "unknown"),
    }


def answer_refuses(answer: str) -> bool:
    return any(marker in (answer or "") for marker in REFUSAL_MARKERS)


def print_report(report: dict[str, Any]):
    print(f"评测模式: {report['mode']}")
    if report.get("model_name"):
        print(f"生成模型: {report['model_name']}")
    print(f"评测样本数: {report['sample_count']}")
    fingerprint_status = report.get("dataset_fingerprint_status")
    if fingerprint_status == "verified":
        print(f"评测数据指纹: {report['dataset_fingerprint']}")
    elif fingerprint_status == "legacy_unbound":
        print("评测数据指纹: 旧结果未绑定（仅兼容读取）")
    else:
        print("评测数据指纹: 不一致")
    print(f"检索全命中率@{report['top_k']}: {_percent(report['retrieval_all_hit_rate'])}")
    print(f"检索任一命中率@{report['top_k']}: {_percent(report['retrieval_any_hit_rate'])}")
    print(f"首条命中率@1: {_percent(report['first_hit_rate'])}")
    confidence = report["retrieval_confidence_intervals_95"]
    print(
        "检索全命中率 95% CI: "
        f"{_interval(confidence['retrieval_all_hit_rate'])}"
    )
    print(f"平均倒数排名(MRR): {_decimal(report['mean_reciprocal_rank'])}")
    print(f"平均来源覆盖率@{report['top_k']}: {_percent(report['average_source_coverage'])}")
    print(f"上下文精确率@{report['top_k']}: {_percent(report['average_context_precision'])}")
    print(f"跨文档全命中率@{report['top_k']}: {_percent(report['multi_doc_all_hit_rate'])}")
    print(f"图谱相关题来源命中率@{report['top_k']}: {_percent(report['graph_source_hit_rate'])}")
    if report.get("graph_observability_sample_count"):
        print(
            "图检索路由选择率/实际执行率/上下文非空率: "
            f"{_percent(report['graph_route_selected_rate'])} / "
            f"{_percent(report['graph_stage_executed_rate'])} / "
            f"{_percent(report['graph_context_nonempty_rate'])}"
        )
    print(f"引用合法率: {_percent(report['citation_valid_rate'])}")
    print(f"引用异常率: {_percent(report['invalid_citation_rate'])}")
    print(f"引用内容支持率: {_percent(report['citation_support_valid_rate'])}")
    print(f"引用内容不支持率: {_percent(report['citation_support_invalid_rate'])}")
    print(f"已检查 claim 数: {report['claim_support_checked_count']}")
    print(f"不支持 claim 数: {report['claim_support_unsupported_claim_count']}")
    print(f"拒答正确率: {_percent(report['refusal_accuracy'])}")
    print(f"拒答漏放率: {_percent(report['refusal_leak_rate'])}")
    print(f"误拒率: {_percent(report['false_refusal_rate'])}")
    if report.get("scoped_sample_count"):
        print(f"项目作用域样本数: {report['scoped_sample_count']}")
        print(f"错误项目率: {_percent(report['wrong_project_rate'])}")
    if report.get("workflow_sample_count"):
        print(f"研究窗口样本数: {report['workflow_sample_count']}")
        print(
            "研究窗口输出契约通过率: "
            f"{_percent(report['workflow_output_contract_pass_rate'])}"
        )
        for workflow_id, workflow_report in report.get("workflow_breakdown", {}).items():
            print(
                f"- {workflow_id}: {workflow_report['sample_count']} 条，"
                "检索任一命中率 "
                f"{_percent(workflow_report['retrieval_any_hit_rate'])}，"
                "输出契约通过率 "
                f"{_percent(workflow_report['workflow_output_contract_pass_rate'])}"
            )
    print(f"可回答题达标率: {_percent(report['answerable_pass_rate'])}")
    print(f"答案必需来源引用全命中率: {_percent(report['answer_expected_source_all_hit_rate'])}")
    print(f"答案必需来源引用覆盖率: {_percent(report['answer_expected_source_coverage'])}")
    print(f"综合可靠率: {_percent(report['overall_reliability_rate'])}")
    print(f"答案关键词覆盖率: {_percent(report['answer_keyword_coverage'])}")
    if report["avg_latency_ms"] is not None:
        latency_label = "平均端到端耗时" if report["answer_sample_count"] else "平均检索耗时"
        print(f"{latency_label}: {report['avg_latency_ms']} ms")
    if report["avg_generation_ms"] is not None:
        print(f"平均答案生成耗时: {report['avg_generation_ms']} ms")

    misses = [row for row in report["rows"] if row["expected_sources"] and not row["source_all_hit"]]
    if misses:
        print("\n未命中样本:")
        for row in misses[:10]:
            expected = " / ".join(row["expected_sources"])
            actual = " / ".join(row["top_sources"][:3]) or "无证据"
            print(f"- {row['id']}: 期望 {expected}; 实际 {actual}")


def print_comparison_report(
    baseline_report: dict[str, Any],
    candidate_report: dict[str, Any],
    baseline_label: str,
    candidate_label: str,
):
    print(f"对比报告: {baseline_label} -> {candidate_label}")
    if baseline_report.get("model_name") or candidate_report.get("model_name"):
        print(f"模型: {baseline_report.get('model_name') or baseline_label} -> {candidate_report.get('model_name') or candidate_label}")
    top_k = candidate_report.get("top_k") or baseline_report.get("top_k")
    latency_label = (
        "平均端到端耗时"
        if baseline_report.get("answer_sample_count") or candidate_report.get("answer_sample_count")
        else "平均检索耗时"
    )
    comparison_items = [
        (f"检索全命中率@{top_k}", "retrieval_all_hit_rate", "higher"),
        (f"检索任一命中率@{top_k}", "retrieval_any_hit_rate", "higher"),
        ("首条命中率@1", "first_hit_rate", "higher"),
        ("MRR", "mean_reciprocal_rank", "higher_decimal"),
        (f"平均来源覆盖率@{top_k}", "average_source_coverage", "higher"),
        (f"上下文精确率@{top_k}", "average_context_precision", "higher"),
        (f"跨文档全命中率@{top_k}", "multi_doc_all_hit_rate", "higher"),
        (f"图谱相关题来源命中率@{top_k}", "graph_source_hit_rate", "higher"),
        ("图检索路由选择率", "graph_route_selected_rate", "higher"),
        ("图检索实际执行率", "graph_stage_executed_rate", "higher"),
        ("图上下文非空率", "graph_context_nonempty_rate", "higher"),
        ("综合可靠率", "overall_reliability_rate", "higher"),
        ("可回答题达标率", "answerable_pass_rate", "higher"),
        ("答案必需来源引用全命中率", "answer_expected_source_all_hit_rate", "higher"),
        ("答案必需来源引用覆盖率", "answer_expected_source_coverage", "higher"),
        ("拒答正确率", "refusal_accuracy", "higher"),
        ("拒答漏放率", "refusal_leak_rate", "lower"),
        ("误拒率", "false_refusal_rate", "lower"),
        ("错误项目率", "wrong_project_rate", "lower"),
        ("研究窗口输出契约通过率", "workflow_output_contract_pass_rate", "higher"),
        ("引用合法率", "citation_valid_rate", "higher"),
        ("引用异常率", "invalid_citation_rate", "lower"),
        ("引用内容支持率", "citation_support_valid_rate", "higher"),
        ("引用内容不支持率", "citation_support_invalid_rate", "lower"),
        ("答案关键词覆盖率", "answer_keyword_coverage", "higher"),
        (latency_label, "avg_latency_ms", "lower_ms"),
        ("平均检索阶段耗时", "avg_retrieval_ms", "lower_ms"),
        ("平均答案生成耗时", "avg_generation_ms", "lower_ms"),
    ]
    for label, key, direction in comparison_items:
        before = baseline_report.get(key)
        after = candidate_report.get(key)
        if before is None and after is None:
            continue
        print(f"{label}: {_format_metric(before, direction)} -> {_format_metric(after, direction)} ({_format_delta(before, after, direction)})")
    paired = paired_binary_comparison(
        baseline_report.get("rows") or [],
        candidate_report.get("rows") or [],
        "source_all_hit",
    )
    print(
        "配对全命中变化: "
        f"改善={paired['improved']}, 退化={paired['regressed']}, "
        f"不变={paired['unchanged']}, exact McNemar p={paired['p_value']:.6f}"
    )


def paired_binary_comparison(
    baseline_rows: list[dict[str, Any]],
    candidate_rows: list[dict[str, Any]],
    key: str,
) -> dict[str, int | float]:
    baseline_by_id = {
        str(row.get("id") or ""): bool(row.get(key))
        for row in baseline_rows
        if row.get("id")
    }
    candidate_by_id = {
        str(row.get("id") or ""): bool(row.get(key))
        for row in candidate_rows
        if row.get("id")
    }
    shared_ids = sorted(set(baseline_by_id) & set(candidate_by_id))
    improved = sum(
        not baseline_by_id[row_id] and candidate_by_id[row_id]
        for row_id in shared_ids
    )
    regressed = sum(
        baseline_by_id[row_id] and not candidate_by_id[row_id]
        for row_id in shared_ids
    )
    unchanged = len(shared_ids) - improved - regressed
    discordant = improved + regressed
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(
            math.comb(discordant, successes)
            for successes in range(min(improved, regressed) + 1)
        ) / (2**discordant)
        p_value = min(1.0, 2 * tail)
    return {
        "sample_count": len(shared_ids),
        "improved": improved,
        "regressed": regressed,
        "unchanged": unchanged,
        "p_value": round(p_value, 6),
    }


def _percent(value: float | None) -> str:
    if value is None:
        return "无样本"
    return f"{value * 100:.1f}%"


def _decimal(value: float | None) -> str:
    if value is None:
        return "无样本"
    return f"{value:.3f}"


def _interval(value: dict[str, Any]) -> str:
    low = value.get("low")
    high = value.get("high")
    if low is None or high is None:
        return "无样本"
    return f"[{float(low) * 100:.1f}%, {float(high) * 100:.1f}%]"


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value)
    return value if value else None


def _parse_workflow_id(value: Any, *, line_number: int | None = None) -> str | None:
    workflow_id = str(value).strip() if value is not None else ""
    if not workflow_id:
        return None
    if workflow_id not in SUPPORTED_WORKFLOW_IDS:
        location = f"评测数据第 {line_number} 行" if line_number else "评测结果"
        allowed = ", ".join(sorted(SUPPORTED_WORKFLOW_IDS))
        raise ValueError(
            f"{location} workflow_id 无效: {workflow_id}；"
            f"只允许已注册的研究窗口: {allowed}"
        )
    return workflow_id


def _format_metric(value: float | int | None, direction: str) -> str:
    if value is None:
        return "无样本"
    if direction.endswith("_ms"):
        return f"{int(value)} ms"
    if direction.endswith("_decimal"):
        return _decimal(float(value))
    return _percent(float(value))


def _format_delta(before: float | int | None, after: float | int | None, direction: str) -> str:
    if before is None or after is None:
        return "无法比较"
    delta = after - before
    if direction.endswith("_ms"):
        sign = "+" if delta >= 0 else ""
        return f"{sign}{int(delta)} ms"
    if direction.endswith("_decimal"):
        sign = "+" if delta >= 0 else ""
        return f"{sign}{delta:.3f}"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta * 100:.1f}百分点"


if __name__ == "__main__":
    configure_utf8_stdio()
    raise SystemExit(main(sys.argv[1:]))
