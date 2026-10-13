"""Business-first multimodal workflows for the local research workbench.

The planner is deliberately deterministic.  It does not call a model and it
does not execute side effects; it only answers three questions before the
expensive stages run: what the user is trying to finish, whether the required
inputs are present, and what evidence/output contract will be used.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


CITATION_PATTERN = re.compile(r"\[((?:E|V|A|M)\d+)\]")


WORKFLOW_CATALOG: dict[str, dict[str, Any]] = {
    "paper_review": {
        "workflow_id": "paper_review",
        "playbook_version": "1.0",
        "title": "论文精读 → 组会材料",
        "audience": "高校科研人员",
        "description": "把选中的论文整理成研究问题、方法、结果、局限和可汇报提纲。",
        "input_contract": [
            "当前项目空间（必需）",
            "至少一篇已索引论文（必需）",
            "当前授权屏幕选区（可选）",
            "已确认语音补充要求（可选）",
        ],
        "input_labels": ["至少一篇已索引论文", "可选：当前页/选区", "可选：语音补充要求"],
        "output": "证据式论文摘要、关键结论与组会汇报提纲",
        "evidence_plan": ["选中文档的章节、页码和实验结果", "用户明确授权的当前屏幕选区"],
        "side_effects": ["只读取当前项目和本轮授权输入", "生成 PPT 前仍需单独确认"],
        "confirmation_points": ["确认论文范围", "确认汇报口径后再生成 PPT"],
        "response_contract": {
            "required_sections": [
                "研究问题",
                "核心方法",
                "实验结果",
                "局限与复现",
                "汇报提纲",
            ],
                "evidence_rules": [
                    "论文事实、方法、数字和结论使用可定位的 [E#] 引用",
                    "当前截图中可见内容才可使用 [V#]，不能替代论文证据",
                    "缺少论文证据的内容标记为待确认，不要补猜",
                ],
                "allowed_citation_prefixes": ["E", "V"],
                "missing_evidence_marker": "待确认",
                "session_instruction": "这是持续绑定到当前会话的论文精读模式。普通文本可以直接作为论文问题或追问；只使用当前项目与用户选中文档的证据，无法证明的内容标记为待确认。",
            },
        "retrieval_policy": {
            "profile": "paper_review",
            "requires_source_diversity": False,
            "query_focuses": [
                "研究问题 研究目标 研究假设 核心方法 方法流程",
                "实验结果 数据集 评测指标 局限 复现",
            ],
        },
        "suggested_query": "请精读当前选中的论文，按研究问题、核心方法、实验结果、局限与可复现实验整理，并为每个事实标注证据。",
    },
    "experiment_compare": {
        "workflow_id": "experiment_compare",
        "playbook_version": "1.0",
        "title": "实验对比 → 问题定位",
        "audience": "高校实验室与企业研发",
        "description": "对比两份实验记录或当前授权表格，区分已证实变化、候选原因和下一步最小验证实验。",
        "input_contract": [
            "当前项目空间（必需）",
            "两份实验材料、一张明确授权的对比表格，或两次已选定且可比的实验运行（必需）",
            "语音说明关注指标（可选）",
        ],
        "input_labels": ["至少两份实验材料、一张明确授权的对比表格，或两次可比实验运行", "可选：语音说明关注指标"],
        "output": "指标差异表、证据定位、候选原因与下一步验证计划",
        "evidence_plan": ["两份实验报告中的同名指标、数据集和配置", "授权屏幕中的表格/OCR区域", "通过可比性门的结构化实验测量 M#"],
        "side_effects": ["只读取当前项目和本轮授权输入", "不自动修改实验记录或执行验证实验"],
        "confirmation_points": ["确认两份材料确属同一指标定义", "确认候选原因不是已证实结论"],
        "response_contract": {
            "required_sections": [
                "结论",
                "已证实变化",
                "高可信候选原因",
                "待验证假设",
                "最小验证实验",
            ],
                "evidence_rules": [
                    "数值、单位、指标、版本和变化使用 [M#] 或可定位的 [E#] 引用",
                    "截图只支持当前画面可见内容，使用 [V#]，不得据此声称文件或实验事实",
                    "原因没有对照实验支持时必须标为高可信候选或待验证假设",
                ],
                "allowed_citation_prefixes": ["E", "V", "M"],
                "missing_evidence_marker": "待确认",
                "session_instruction": "这是持续绑定到当前会话的实验对比模式。普通文本可以直接作为分析问题；只有显式可比的测量才能写成已证实变化，原因必须区分候选因素、待验证假设和最小验证实验。",
            },
        "retrieval_policy": {
            "profile": "experiment_compare",
            "requires_source_diversity": True,
            "query_focuses": [
                "指标 数值 单位 数据集 评测脚本",
                "版本 配置 环境 变化 候选原因 验证实验",
            ],
        },
        "suggested_query": "请对比当前实验材料，先列出可直接由证据证明的变化，再区分高可信候选原因和待验证假设，最后给出最小验证实验。",
    },
    "meeting_actions": {
        "workflow_id": "meeting_actions",
        "playbook_version": "1.0",
        "title": "语音/阅读记录 → 行动项",
        "audience": "课题组会议与企业项目协作",
        "description": "把用户主动提供的语音、转写或直接记录文本整理成决策、待办、负责人、截止时间和草稿产物。",
        "input_contract": [
            "当前项目空间（必需）",
            "语音录音、已确认转写，或在本会话中直接输入的会议/阅读记录（至少一种）",
            "会议材料或当前屏幕（可选）",
        ],
        "input_labels": ["语音、确认转写或直接记录文本", "可选：会议材料/当前屏幕", "外发邮件和写入日历必须二次确认"],
        "output": "结构化会议纪要、待办清单、邮件/日程草稿",
        "evidence_plan": ["本轮用户确认的语音转写", "当前项目内相关文档与会话上下文"],
        "side_effects": ["默认只生成本地草稿", "外发邮件或写入日历必须二次确认"],
        "confirmation_points": ["确认转写和责任人/时间", "确认后才生成邮件或日程草稿"],
        "response_contract": {
            "required_sections": [
                "会议结论",
                "行动项",
                "待确认信息",
                "草稿边界",
            ],
                "evidence_rules": [
                    "会议记录中的事实、决定和行动项使用用户确认的 [A#] 引用",
                    "文档事实使用可定位的 [E#] 引用，截图直接观察使用 [V#] 引用",
                    "负责人、截止时间或决定不明确时写入待确认，不要从语气推断",
                ],
                "allowed_citation_prefixes": ["A", "E", "V"],
                "missing_evidence_marker": "待确认",
                "session_instruction": "这是持续绑定到当前会话的会议纪要模式。普通文字消息按用户提供的会议记录文本处理，不得伪装成语音转写；负责人、截止时间和决定不明确时标记为待确认，外发邮件或写入日历仍需单独确认。",
            },
        "retrieval_policy": {
            "profile": "meeting_record_first",
            "requires_source_diversity": False,
            "query_focuses": [
                "会议结论 决策",
                "行动项 负责人 截止时间 待确认",
            ],
        },
        "suggested_query": "请把本轮语音或阅读记录整理为会议纪要、明确的行动项、负责人和截止时间；不确定的信息标记为待确认。",
    },
}


def list_workflow_specs(
    approvals: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return UI-safe copies so callers cannot mutate the catalog."""
    return [
        {
            **deepcopy(spec),
            "approval": playbook_approval_state(spec["workflow_id"], (approvals or {}).get(spec["workflow_id"])),
        }
        for spec in WORKFLOW_CATALOG.values()
    ]


def get_workflow_spec(workflow_id: str) -> dict[str, Any]:
    workflow_key = str(workflow_id or "").strip()
    spec = WORKFLOW_CATALOG.get(workflow_key)
    if spec is None:
        raise ValueError(f"unknown multimodal workflow: {workflow_id}")
    return deepcopy(spec)


def workflow_response_contract(workflow_id: str | None) -> dict[str, Any] | None:
    """Return the immutable-by-copy output contract for a known Playbook."""
    workflow_key = str(workflow_id or "").strip()
    if not workflow_key:
        return None
    spec = get_workflow_spec(workflow_key)
    contract = spec.get("response_contract")
    return deepcopy(contract) if isinstance(contract, dict) else None


def workflow_retrieval_policy(workflow_id: str | None) -> dict[str, Any]:
    """Return the deterministic retrieval policy for a bound Playbook."""
    workflow_key = str(workflow_id or "").strip()
    if not workflow_key:
        return {
            "profile": "default",
            "requires_source_diversity": False,
        }
    spec = get_workflow_spec(workflow_key)
    policy = spec.get("retrieval_policy")
    if not isinstance(policy, dict):
        return {
            "profile": "default",
            "requires_source_diversity": False,
        }
    return deepcopy(policy)


def workflow_retrieval_variants(
    workflow_id: str | None,
    user_query: str,
    *,
    limit: int = 2,
) -> list[str]:
    """Build bounded, deterministic retrieval focuses for a bound Playbook.

    The focus terms are a small local equivalent of a research workflow's
    question plan.  They influence candidate recall only; the original user
    query remains the evidence-gate and answer contract.
    """
    query = " ".join(str(user_query or "").split())[:6000]
    if not query:
        return []
    policy = workflow_retrieval_policy(workflow_id)
    focuses = policy.get("query_focuses")
    if not isinstance(focuses, list):
        return []
    variants: list[str] = []
    seen = {query}
    bounded_limit = max(0, min(int(limit), 3))
    for raw_focus in focuses:
        focus = " ".join(str(raw_focus or "").split())[:320]
        if not focus:
            continue
        variant = f"{query}\n检索重点：{focus}"[:6000]
        if variant in seen:
            continue
        seen.add(variant)
        variants.append(variant)
        if len(variants) >= bounded_limit:
            break
    return variants


def evaluate_workflow_output_contract(
    workflow_id: str | None,
    answer: str,
) -> dict[str, Any]:
    """Check required sections have visible content without judging facts."""
    contract = workflow_response_contract(workflow_id)
    if not contract:
        return {
            "status": "not_applicable",
            "required_sections": [],
            "missing_sections": [],
        }

    required = [
        str(item).strip()
        for item in contract.get("required_sections") or []
        if str(item).strip()
    ]
    observed: set[str] = set()
    section_content: dict[str, list[str]] = {title: [] for title in required}
    current_section: str | None = None
    for raw_line in str(answer or "").splitlines():
        line = re.sub(r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)、．]?\s*)", "", raw_line).strip()
        matched_section: str | None = None
        inline_content = ""
        for title in required:
            if line == title:
                matched_section = title
                break
            for separator in ("：", ":"):
                prefix = f"{title}{separator}"
                if line.startswith(prefix):
                    matched_section = title
                    inline_content = line[len(prefix):].strip()
                    break
            if matched_section:
                break
        if matched_section:
            observed.add(matched_section)
            current_section = matched_section
            if inline_content:
                section_content[matched_section].append(inline_content)
            continue
        if current_section and line:
            section_content[current_section].append(line)

    empty_sections = [
        title
        for title in required
        if title in observed and not section_content[title]
    ]
    missing = [
        title
        for title in required
        if title not in observed or title in empty_sections
    ]
    return {
        "status": "pass" if not missing else "incomplete",
        "required_sections": required,
        "observed_sections": [title for title in required if title in observed],
        "missing_sections": missing,
        "empty_sections": empty_sections,
        "missing_evidence_marker": str(contract.get("missing_evidence_marker") or "待确认"),
    }


def validate_workflow_citation_namespaces(
    workflow_id: str | None,
    answer: str,
) -> dict[str, Any]:
    """Enforce each Playbook's allowed evidence namespaces.

    Citation IDs are still checked against the admitted evidence separately by
    ``validate_answer_citations``.  This narrower contract prevents a valid
    but semantically wrong evidence kind from crossing a Playbook boundary.
    """
    contract = workflow_response_contract(workflow_id)
    if not contract:
        return {
            "valid": True,
            "status": "not_applicable",
            "allowed_citation_prefixes": [],
            "disallowed_citations": [],
        }

    allowed = {
        str(prefix).strip().upper()[:1]
        for prefix in contract.get("allowed_citation_prefixes") or []
        if str(prefix).strip()
    }
    cited = list(dict.fromkeys(CITATION_PATTERN.findall(str(answer or ""))))
    disallowed = [citation for citation in cited if citation[:1].upper() not in allowed]
    return {
        "valid": not disallowed,
        "status": "pass" if not disallowed else "retry",
        "allowed_citation_prefixes": sorted(allowed),
        "cited_citations": cited,
        "disallowed_citations": disallowed,
        "reason": (
            "workflow citation namespace is not allowed"
            if disallowed
            else "workflow citation namespaces validated"
        ),
    }


def playbook_approval_state(
    workflow_id: str,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = get_workflow_spec(workflow_id)
    record = approval or {}
    expected_version = str(spec["playbook_version"])
    status = str(record.get("status") or "unreviewed").strip().lower()
    version = str(record.get("playbook_version") or "").strip()
    if status == "approved" and version == expected_version:
        effective_status = "approved"
    elif status == "revoked":
        effective_status = "revoked"
    elif version and version != expected_version:
        effective_status = "stale"
    else:
        effective_status = "unreviewed"
    return {
        "status": effective_status,
        "playbook_version": expected_version,
        "reviewed_at": str(record.get("reviewed_at") or ""),
    }


def plan_workflow(
    workflow_id: str,
    *,
    project_id: str | None = None,
    document_names: list[str] | None = None,
    document_ids: list[str] | None = None,
    has_audio: bool = False,
    has_image: bool = False,
    speech_text: str = "",
    text_note: str = "",
    selected_text: str = "",
    baseline_run_id: str | None = None,
    current_run_id: str | None = None,
    approval: dict[str, Any] | None = None,
    require_approval: bool = False,
) -> dict[str, Any]:
    """Build a deterministic readiness/evidence plan for one business task."""
    workflow_key = str(workflow_id or "").strip()
    spec = get_workflow_spec(workflow_key)
    approval_state = playbook_approval_state(workflow_key, approval)

    documents = _unique_names(document_names or [])
    selected_document_ids = []
    for item in document_ids or []:
        document_id = " ".join(str(item or "").strip().split())[:256]
        if document_id and document_id not in selected_document_ids:
            selected_document_ids.append(document_id)
        if len(selected_document_ids) >= 12:
            break
    selected_document_count = max(len(documents), len(selected_document_ids))
    has_transcript = bool(str(speech_text or "").strip())
    has_text_note = bool(str(text_note or "").strip())
    has_selected_text = bool(str(selected_text or "").strip())
    has_experiment_comparison = bool(str(baseline_run_id or "").strip() and str(current_run_id or "").strip())
    has_partial_experiment_selection = bool(str(baseline_run_id or "").strip()) != bool(str(current_run_id or "").strip())
    missing: list[str] = []
    if not str(project_id or "").strip():
        missing.append("请先选择项目空间")
    elif require_approval and approval_state["status"] != "approved":
        missing.append("请先审核通过当前项目的 Playbook")

    if workflow_key == "paper_review" and not selected_document_count:
        missing.append("至少选择一篇已索引论文")
    if workflow_key == "experiment_compare":
        if has_partial_experiment_selection:
            missing.append("请同时选择基线运行和当前运行")
        elif selected_document_count < 2 and not (has_image or has_selected_text or has_experiment_comparison):
            missing.append("至少选择两份实验材料、授权一张包含两组数据的屏幕选区，或选择两次实验运行")
    if workflow_key == "meeting_actions" and not (has_audio or has_transcript or has_text_note):
        missing.append("请提供语音录音、确认转写，或直接输入会议/阅读记录")

    if workflow_key == "paper_review":
        route = "auto" if has_image else "none"
    elif workflow_key == "experiment_compare":
        route = "slow" if has_image else "none"
    else:
        route = "auto" if has_image else "none"

    evidence_scope = list(documents)
    if has_image:
        evidence_scope.append("本轮授权屏幕选区")
    if has_audio or has_transcript:
        evidence_scope.append("本轮确认语音/转写")
    if has_text_note:
        evidence_scope.append("本轮用户提供的会议/阅读记录文本")
    if has_experiment_comparison:
        evidence_scope.append("结构化实验对比（M#）")

    return {
        "workflow_id": workflow_key,
        "playbook_id": workflow_key,
        "playbook_version": spec["playbook_version"],
        "title": spec["title"],
        "ready": not missing,
        "missing_inputs": missing,
        "input_contract": list(spec["input_contract"]),
        "input_state": {
            "project_selected": bool(str(project_id or "").strip()),
            "document_count": selected_document_count,
            "document_id_count": len(selected_document_ids),
            "has_audio": bool(has_audio),
            "has_image": bool(has_image),
            "has_transcript": has_transcript,
            "has_text_note": has_text_note,
            "has_selected_text": has_selected_text,
            "has_experiment_comparison": has_experiment_comparison,
        },
        "routing": {
            "visual_mode": route,
            "needs_rag": workflow_key != "meeting_actions" or bool(selected_document_count),
            "needs_confirmation": True,
        },
        "approval": approval_state,
        "evidence_scope": evidence_scope,
        "output": spec["output"],
        "response_contract": workflow_response_contract(workflow_key),
        "evidence_plan": list(spec["evidence_plan"]),
        "side_effects": list(spec["side_effects"]),
        "confirmation_points": list(spec["confirmation_points"]),
        "suggested_query": spec["suggested_query"],
        "next_action": "可以开始执行" if not missing else "；".join(missing),
    }


def build_workflow_query(workflow_id: str | None, user_query: str) -> str:
    """Add a short, auditable task contract to the RAG query."""
    query = str(user_query or "").strip()
    if not workflow_id:
        return query
    spec = get_workflow_spec(workflow_id)
    return (
        f"【当前业务任务：{spec['title']}】\n"
        f"输出要求：{spec['output']}。\n"
        f"会话模式约束：{spec.get('response_contract', {}).get('session_instruction', '')}\n"
        "仅使用可定位证据；无法证明的内容标记为待确认，不要猜测。\n"
        f"用户原始要求：{query}"
    )


def _unique_names(values: list[str]) -> list[str]:
    names: list[str] = []
    for value in values[:12]:
        name = str(value or "").strip()[:260]
        if name and name not in names:
            names.append(name)
    return names
