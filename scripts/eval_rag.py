import argparse
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.runtime_state import get_runtime_state
from core.evaluation import wilson_interval
from core.rag_defaults import DEFAULT_RAG_TOP_K
from core.security_context import WORKSPACE_DATA_KEY_ENV, get_workspace_keys, restore_workspace_keys, set_workspace_keys
from core.workspace import WORKSPACE_MANIFEST, SoulDriveWorkspace
from core.workspace_crypto import unlock_keystore

CITATION_PATTERN = re.compile(r"\[E(\d+)\]")
REFUSAL_MARKERS = (
    "未找到足够可靠",
    "没有足够",
    "无法回答",
    "不能回答",
    "不会生成可能误导",
    "无相关证据",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate SoulDrive retrieval and citation quality.")
    parser.add_argument("--dataset", default="eval/enterprise_zh_v0.2.jsonl", help="JSONL evaluation dataset path.")
    parser.add_argument("--workspace", default=None, help="SoulDrive workspace root. Defaults to current runtime workspace.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_RAG_TOP_K, help="Evidence count to retrieve.")
    parser.add_argument("--include-answers", action="store_true", help="Run full RAG answer generation, not retrieval only.")
    parser.add_argument("--chat-model", default=None, help="Temporarily set SOULDRIVE_CHAT_MODEL for this evaluation run.")
    parser.add_argument("--from-results", default=None, help="Read prior JSONL results instead of running retrieval.")
    parser.add_argument("--save-results", default=None, help="Write JSONL retrieval results for later comparison.")
    parser.add_argument("--compare-results", nargs=2, metavar=("BASELINE_JSONL", "CANDIDATE_JSONL"), help="Compare two saved result files.")
    parser.add_argument("--baseline-label", default="baseline", help="Label for the first --compare-results file.")
    parser.add_argument("--candidate-label", default="candidate", help="Label for the second --compare-results file.")
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
            if args.include_answers:
                results = run_answer_generation(records, workspace=workspace, top_k=args.top_k)
                mode = "答案生成评测"
            else:
                results = run_retrieval(records, workspace=workspace, top_k=args.top_k)
                mode = "检索评测"

        if args.save_results:
            save_results(Path(args.save_results), results)

        report = summarize(records, results, top_k=args.top_k, mode=mode)
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
            )
        )
    return records


def load_results(path: Path, records: list[EvalRecord]) -> list[EvalResult]:
    by_id: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            payload = json.loads(line)
            by_id[str(payload["id"])] = payload

    results = []
    for record in records:
        payload = by_id.get(record.id, {})
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
            )
        )
    return results


def run_retrieval(records: list[EvalRecord], workspace: str | None, top_k: int) -> list[EvalResult]:
    from core.knowledge_base import LocalKnowledgeBase

    if not workspace:
        raise ValueError("workspace is required for live retrieval")
    kb = LocalKnowledgeBase(workspace_path=workspace)
    results = []
    try:
        for index, record in enumerate(records, start=1):
            if index == 1 or index % 10 == 0 or index == len(records):
                print(f"[{index}/{len(records)}] 检索评测: {record.id}", flush=True)
            started_at = time.time()
            retrieval = kb.search_with_evidence(record.question, top_k=top_k)
            elapsed_ms = int((time.time() - started_at) * 1000)
            results.append(
                EvalResult(
                    id=record.id,
                    question=record.question,
                    expected_sources=record.expected_sources,
                    answer="",
                    evidence=list(retrieval.get("evidence", [])),
                    elapsed_ms=elapsed_ms,
                )
            )
    finally:
        kb.close()
    return results


def run_answer_generation(records: list[EvalRecord], workspace: str | None, top_k: int) -> list[EvalResult]:
    from core.knowledge_base import LocalKnowledgeBase
    from core.rag_engine import RAGEngine

    if not workspace:
        raise ValueError("workspace is required for answer generation")
    workspace_obj = SoulDriveWorkspace(workspace).ensure()
    kb = LocalKnowledgeBase(workspace_path=workspace)
    rag = None
    results = []
    try:
        rag = RAGEngine(
            kb=kb,
            graph_db_path=workspace_obj.graph_db_path,
            workspace_path=workspace_obj.root_path,
        )
        model_name = getattr(rag.runtime_config, "model_filename", None)
        for index, record in enumerate(records, start=1):
            print(f"[{index}/{len(records)}] 生成答案: {record.id}", flush=True)
            started_at = time.time()
            stream_text = "".join(rag.generate_response_stream(record.question, top_k=top_k))
            elapsed_ms = int((time.time() - started_at) * 1000)
            answer, evidence = split_answer_and_evidence(stream_text)
            results.append(
                EvalResult(
                    id=record.id,
                    question=record.question,
                    expected_sources=record.expected_sources,
                    answer=answer,
                    evidence=evidence,
                    elapsed_ms=elapsed_ms,
                    model_name=model_name,
                )
            )
    finally:
        if rag is not None:
            rag.close()
        kb.close()
    return results


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
        runtime_workspace,
        find_unlocked_drive_workspace(),
        str(PROJECT_ROOT / "souldrive_db"),
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

    positive_rows = [row for row in rows if row["expected_sources"]]
    answer_rows = [row for row in rows if row["has_answer"]]
    refusal_rows = [row for row in rows if row["should_refuse"] and row["has_answer"]]
    keyword_rows = [row for row in rows if row["keyword_sample"]]
    answerable_rows = [row for row in rows if row["has_answer"] and not row["should_refuse"]]
    multi_doc_rows = [row for row in rows if row["requires_multi_doc"]]
    graph_rows = [row for row in rows if row["graph_relevant"]]
    latencies = [row["elapsed_ms"] for row in rows if row["elapsed_ms"] is not None]
    retrieval_latencies = [row["retrieval_ms"] for row in rows if row["retrieval_ms"] is not None]
    generation_latencies = [row["generation_ms"] for row in rows if row["generation_ms"] is not None]
    model_names = sorted({row["model_name"] for row in rows if row["model_name"]})
    answer_sample_count = len(answer_rows)

    retrieval_all_hits = sum(row["source_all_hit"] for row in positive_rows)
    retrieval_any_hits = sum(row["source_any_hit"] for row in positive_rows)
    first_hits = sum(row["first_hit"] for row in positive_rows)
    multi_doc_hits = sum(row["source_all_hit"] for row in multi_doc_rows)
    graph_hits = sum(row["source_any_hit"] for row in graph_rows)
    return {
        "mode": mode or ("答案生成评测" if answer_sample_count else "检索评测"),
        "sample_count": len(rows),
        "answer_sample_count": answer_sample_count,
        "top_k": top_k,
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
        "multi_doc_all_hit_rate": _ratio(multi_doc_hits, len(multi_doc_rows)),
        "graph_context_hit_rate": _ratio(graph_hits, len(graph_rows)),
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
        "refusal_accuracy": _ratio(sum(row["refusal_correct"] for row in refusal_rows), len(refusal_rows)),
        "refusal_leak_rate": _ratio(sum(row["refusal_leak"] for row in refusal_rows), len(refusal_rows)),
        "false_refusal_rate": _ratio(sum(row["false_refusal"] for row in answerable_rows), len(answerable_rows)),
        "answerable_pass_rate": _ratio(sum(row["answerable_pass"] for row in answerable_rows), len(answerable_rows)),
        "overall_reliability_rate": _ratio(sum(row["reliable_answer"] for row in answer_rows), len(answer_rows)),
        "answer_keyword_coverage": _ratio(sum(row["keyword_coverage"] for row in keyword_rows), len(keyword_rows)),
        "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else None,
        "avg_retrieval_ms": int(sum(retrieval_latencies) / len(retrieval_latencies)) if retrieval_latencies else None,
        "avg_generation_ms": int(sum(generation_latencies) / len(generation_latencies)) if generation_latencies else None,
        "rows": rows,
    }


def score_record(record: EvalRecord, result: EvalResult, top_k: int) -> dict[str, Any]:
    evidence = result.evidence[:top_k]
    answer = result.answer or ""
    expected_sources = record.expected_sources
    source_hits = [
        any(source_matches(item.get("source_filename"), expected) for item in evidence)
        for expected in expected_sources
    ]
    first_evidence = evidence[0] if evidence else {}
    first_hit = bool(expected_sources) and any(
        source_matches(first_evidence.get("source_filename"), expected)
        for expected in expected_sources
    )
    first_match_rank = first_matching_rank(evidence, expected_sources)
    source_coverage = (sum(source_hits) / len(expected_sources)) if expected_sources else 0.0
    citation_valid = citations_are_valid(answer, evidence)
    answer_refused = answer_refuses(answer)
    refusal_correct = record.should_refuse and answer_refused
    source_ok = (not expected_sources) or any(source_hits)
    keyword_coverage = keyword_coverage_ratio(answer, record.expected_keywords)
    keyword_pass = (not record.expected_keywords) or keyword_coverage >= 0.5
    answerable_pass = (
        bool(answer.strip())
        and not record.should_refuse
        and not answer_refused
        and citation_valid
        and source_ok
        and keyword_pass
    )
    reliable_answer = refusal_correct if record.should_refuse else answerable_pass
    return {
        "id": record.id,
        "question": record.question,
        "expected_sources": expected_sources,
        "top_sources": [str(item.get("source_filename") or "未知来源") for item in evidence],
        "has_answer": bool(answer.strip()),
        "source_all_hit": bool(expected_sources) and all(source_hits),
        "source_any_hit": bool(expected_sources) and any(source_hits),
        "first_hit": first_hit,
        "first_match_rank": first_match_rank,
        "reciprocal_rank": (1.0 / first_match_rank) if first_match_rank else 0.0,
        "source_coverage": source_coverage,
        "citation_valid": citation_valid,
        "refusal_correct": refusal_correct,
        "refusal_leak": bool(record.should_refuse and answer.strip() and not answer_refused),
        "false_refusal": bool(not record.should_refuse and answer.strip() and answer_refused),
        "answerable_pass": answerable_pass,
        "reliable_answer": reliable_answer,
        "should_refuse": record.should_refuse,
        "requires_multi_doc": record.requires_multi_doc,
        "graph_relevant": record.graph_relevant,
        "keyword_sample": bool(answer.strip() and record.expected_keywords and not record.should_refuse),
        "keyword_coverage": keyword_coverage,
        "elapsed_ms": result.elapsed_ms,
        "retrieval_ms": result.retrieval_ms,
        "generation_ms": result.generation_ms,
        "model_name": result.model_name,
    }


def source_matches(source_filename: Any, expected: str) -> bool:
    source = normalize_source_name(str(source_filename or ""))
    for alias in source_aliases(expected):
        normalized_alias = normalize_source_name(alias)
        if normalized_alias and normalized_alias in source:
            return True
    return False


def source_aliases(expected: str) -> list[str]:
    aliases = SOURCE_ALIASES.get(expected, [])
    return [expected, *aliases]


def normalize_source_name(value: str) -> str:
    lowered = value.lower()
    for suffix in (".pdf", ".docx", ".md", ".html", ".txt"):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return re.sub(r"[\s_\-—–·,，。\.《》<>“”\"'‘’()（）【】\[\]：:；;]+", "", lowered)


def first_matching_rank(evidence: list[dict[str, Any]], expected_sources: list[str]) -> int | None:
    if not expected_sources:
        return None
    for rank, item in enumerate(evidence, start=1):
        if any(source_matches(item.get("source_filename"), expected) for expected in expected_sources):
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
    cited_ids = {f"E{match}" for match in CITATION_PATTERN.findall(answer or "")}
    if not answer:
        return True
    if not cited_ids:
        return answer_refuses(answer)
    available_ids = {str(item.get("id")) for item in evidence if item.get("id")}
    return cited_ids <= available_ids


def answer_refuses(answer: str) -> bool:
    return any(marker in (answer or "") for marker in REFUSAL_MARKERS)


def print_report(report: dict[str, Any]):
    print(f"评测模式: {report['mode']}")
    if report.get("model_name"):
        print(f"生成模型: {report['model_name']}")
    print(f"评测样本数: {report['sample_count']}")
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
    print(f"跨文档全命中率@{report['top_k']}: {_percent(report['multi_doc_all_hit_rate'])}")
    print(f"图谱相关题命中率@{report['top_k']}: {_percent(report['graph_context_hit_rate'])}")
    print(f"引用合法率: {_percent(report['citation_valid_rate'])}")
    print(f"引用异常率: {_percent(report['invalid_citation_rate'])}")
    print(f"拒答正确率: {_percent(report['refusal_accuracy'])}")
    print(f"拒答漏放率: {_percent(report['refusal_leak_rate'])}")
    print(f"误拒率: {_percent(report['false_refusal_rate'])}")
    print(f"可回答题达标率: {_percent(report['answerable_pass_rate'])}")
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
        (f"跨文档全命中率@{top_k}", "multi_doc_all_hit_rate", "higher"),
        (f"图谱相关题命中率@{top_k}", "graph_context_hit_rate", "higher"),
        ("综合可靠率", "overall_reliability_rate", "higher"),
        ("可回答题达标率", "answerable_pass_rate", "higher"),
        ("拒答正确率", "refusal_accuracy", "higher"),
        ("拒答漏放率", "refusal_leak_rate", "lower"),
        ("误拒率", "false_refusal_rate", "lower"),
        ("引用合法率", "citation_valid_rate", "higher"),
        ("引用异常率", "invalid_citation_rate", "lower"),
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
