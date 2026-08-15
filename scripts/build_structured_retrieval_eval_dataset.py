"""Build a deterministic adversarial retrieval set for formal documents."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.evaluation import evaluate_retrieval_cases, load_retrieval_eval_cases


DEFAULT_OUTPUT = PROJECT_ROOT / "eval" / "retrieval_structure_v0.4.jsonl"
CASES_PER_CATEGORY = 60

CATEGORIES = (
    "clause_lookup",
    "table_lookup",
    "document_reference",
    "overlap_dedup",
    "cross_source_recall",
    "broad_summary",
)

DOCUMENTS = (
    ("企业数据治理平台建设方案", "企数〔2026〕01号", "project_plan"),
    ("数据中台技术架构设计说明书", "技规〔2026〕02号", "technical_document"),
    ("政务数据共享管理办法", "政数规〔2026〕03号", "government_policy"),
    ("人工智能系统安全技术规范", "GB/T 45001-2026", "standard_document"),
    ("智慧城市项目实施方案", "城建〔2026〕05号", "project_plan"),
    ("信息系统灾备建设技术要求", "Q/TECH 006-2026", "technical_document"),
    ("公共数据授权运营实施细则", "数管规〔2026〕07号", "government_policy"),
    ("敏感数据处理安全标准", "GB/T 45008-2026", "standard_document"),
    ("企业知识库升级项目建议书", "企项〔2026〕09号", "project_plan"),
    ("云平台零信任接入技术方案", "云技〔2026〕10号", "technical_document"),
    ("生成式人工智能服务管理指引", "网信规〔2026〕11号", "government_policy"),
    ("软件交付质量验收规范", "Q/QA 012-2026", "standard_document"),
)

TOPICS = (
    ("数据分类分级", "访问控制"),
    ("模型风险评估", "安全审计"),
    ("系统可用性", "灾备恢复"),
    ("项目里程碑", "验收标准"),
    ("接口性能", "并发容量"),
    ("个人信息保护", "授权管理"),
    ("数据质量", "责任部门"),
    ("部署架构", "资源配置"),
    ("应急响应", "处置时限"),
    ("知识库检索", "召回准确率"),
    ("供应商管理", "交付边界"),
    ("日志留存", "合规检查"),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the 360-case formal-document structural retrieval evaluation set."
    )
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--cases-per-category", type=int, default=CASES_PER_CATEGORY)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--skip-evaluation", action="store_true")
    args = parser.parse_args(argv)

    records = build_records(args.cases_per_category)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            for record in records
        )
        + "\n",
        encoding="utf-8",
    )
    summary = dataset_summary(records)
    if not args.skip_evaluation:
        report = evaluate_retrieval_cases(
            load_retrieval_eval_cases(output_path),
            top_k=args.top_k,
        )
        summary["evaluation"] = {
            key: value
            for key, value in report.items()
            if key not in {"rows", "low_evidence_cases"}
        }
        summary["evaluation"]["low_evidence_count"] = len(
            report["low_evidence_cases"]
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_records(cases_per_category: int = CASES_PER_CATEGORY) -> list[dict]:
    if cases_per_category <= 0:
        raise ValueError("cases_per_category must be positive")

    builders = {
        "clause_lookup": _clause_case,
        "table_lookup": _table_case,
        "document_reference": _document_reference_case,
        "overlap_dedup": _overlap_case,
        "cross_source_recall": _cross_source_case,
        "broad_summary": _broad_summary_case,
    }
    records = []
    sequence = 1
    for category in CATEGORIES:
        for index in range(cases_per_category):
            case_id = f"RSE4-{sequence:03d}"
            records.append(builders[category](case_id, index, cases_per_category))
            sequence += 1
    return records


def dataset_summary(records: list[dict]) -> dict:
    categories: dict[str, int] = {}
    difficulties: dict[str, int] = {}
    for record in records:
        category = str(record["category"])
        difficulty = str(record["difficulty"])
        categories[category] = categories.get(category, 0) + 1
        difficulties[difficulty] = difficulties.get(difficulty, 0) + 1
    return {
        "case_count": len(records),
        "categories": categories,
        "difficulties": difficulties,
        "candidate_count": sum(len(record["candidates"]) for record in records),
    }


def _difficulty(index: int, count: int) -> str:
    ratio = index / max(count, 1)
    if ratio < 0.25:
        return "easy"
    if ratio < 0.50:
        return "medium"
    return "hard"


def _dense_rank(difficulty: str) -> int:
    return {"easy": 1, "medium": 4, "hard": 8}[difficulty]


def _case(
    case_id: str,
    category: str,
    difficulty: str,
    query: str,
    expected_ids: list[str],
    candidates: list[dict],
    expected_groups: list[list[str]] | None = None,
) -> dict:
    record = {
        "id": case_id,
        "category": category,
        "difficulty": difficulty,
        "query": query,
        "expected_ids": expected_ids,
        "candidates": candidates,
    }
    if expected_groups:
        record["expected_groups"] = expected_groups
    return record


def _candidate(
    case_id: str,
    suffix: str,
    content: str,
    document_index: int,
    dense_rank: int,
    **metadata,
) -> dict:
    title, number, profile = DOCUMENTS[document_index % len(DOCUMENTS)]
    candidate_id = f"{case_id}-{suffix}"
    base_metadata = {
        "child_id": candidate_id,
        "source_filename": f"{title}.pdf",
        "document_title": title,
        "document_number": number,
        "document_profile": profile,
        "structure_aware": True,
        "child_excerpt": content[:420],
    }
    base_metadata.update(metadata)
    return {
        "id": candidate_id,
        "content": content,
        "metadata": base_metadata,
        "dense_rank": dense_rank,
        "vector_distance": min(0.99, dense_rank / 10),
    }


def _generic_distractors(
    case_id: str,
    index: int,
    start_rank: int,
    count: int,
) -> list[dict]:
    candidates = []
    for offset in range(count):
        topic, measure = TOPICS[(index + offset + 3) % len(TOPICS)]
        candidates.append(
            _candidate(
                case_id,
                f"d{offset + 1}",
                f"## 一般说明\n本节介绍{topic}的背景，不包含目标条款、表格数值或{measure}结论。",
                index + offset + 3,
                start_rank + offset,
                parent_id=f"{case_id}-d-parent-{offset}",
                child_start_char=offset * 600,
                child_end_char=offset * 600 + 260,
                content_kind="paragraph",
                section="一般说明",
            )
        )
    return candidates


def _clause_case(case_id: str, index: int, count: int) -> dict:
    difficulty = _difficulty(index, count)
    doc_index = index % len(DOCUMENTS)
    topic, measure = TOPICS[index % len(TOPICS)]
    clause = f"{index % 9 + 1}.{index * 3 % 8 + 1}"
    relevant = _candidate(
        case_id,
        "relevant",
        f"## {clause} {topic}\n{clause}条要求责任单位实施{topic}，并以{measure}记录作为验收证据。",
        doc_index,
        _dense_rank(difficulty),
        parent_id=f"{case_id}-target-parent",
        child_start_char=900,
        child_end_char=1160,
        content_kind="clause",
        clause_id=clause,
        clause_ids=clause,
        section=f"{clause} {topic}",
    )
    near_clause = f"{clause.rsplit('.', 1)[0]}.{int(clause.rsplit('.', 1)[1]) + 1}"
    candidates = [
        _candidate(
            case_id,
            "near-clause",
            f"## {near_clause} {topic}\n本条也讨论{topic}和{measure}，但不属于用户指定条款。",
            doc_index,
            1,
            parent_id=f"{case_id}-near-parent",
            child_start_char=1161,
            child_end_char=1410,
            content_kind="clause",
            clause_id=near_clause,
            section=f"{near_clause} {topic}",
        ),
        relevant,
        *_generic_distractors(case_id, index, 2, 8),
    ]
    return _case(
        case_id,
        "clause_lookup",
        difficulty,
        f"请定位{clause}条：其中对{topic}及{measure}证据作了什么具体规定？",
        [relevant["id"]],
        candidates,
    )


def _table_case(case_id: str, index: int, count: int) -> dict:
    difficulty = _difficulty(index, count)
    doc_index = index % len(DOCUMENTS)
    topic, measure = TOPICS[index % len(TOPICS)]
    value = f"{90 + index % 9}%"
    table = (
        f"| 控制项 | 指标值 | 责任部门 | 验收依据 |\n"
        f"|---|---:|---|---|\n"
        f"| {topic} | {value} | 数据管理部 | {measure}记录 |"
    )
    relevant = _candidate(
        case_id,
        "relevant",
        table,
        doc_index,
        _dense_rank(difficulty),
        parent_id=f"{case_id}-table-parent",
        child_start_char=500,
        child_end_char=760,
        content_kind="table",
        section=f"{topic}指标表",
        table_count=1,
    )
    prose = _candidate(
        case_id,
        "prose-negative",
        f"{topic}需要责任部门持续关注指标值与{measure}，具体数值见其他附件。",
        doc_index,
        1,
        parent_id=f"{case_id}-prose-parent",
        child_start_char=100,
        child_end_char=360,
        content_kind="paragraph",
        section=f"{topic}说明",
    )
    wrong_table = _candidate(
        case_id,
        "wrong-table",
        f"| 控制项 | 指标值 |\n|---|---:|\n| 其他指标 | {70 + index % 10}% |",
        doc_index + 1,
        2,
        parent_id=f"{case_id}-wrong-table-parent",
        child_start_char=800,
        child_end_char=980,
        content_kind="table",
        section="其他指标表",
    )
    return _case(
        case_id,
        "table_lookup",
        difficulty,
        f"请从表格中查找{topic}对应的指标值、责任部门和{measure}验收依据。",
        [relevant["id"]],
        [prose, wrong_table, relevant, *_generic_distractors(case_id, index, 3, 7)],
    )


def _document_reference_case(case_id: str, index: int, count: int) -> dict:
    difficulty = _difficulty(index, count)
    doc_index = index % len(DOCUMENTS)
    title, number, _profile = DOCUMENTS[doc_index]
    topic, measure = TOPICS[index % len(TOPICS)]
    relevant = _candidate(
        case_id,
        "relevant",
        f"## 适用范围\n{topic}应覆盖建设、运行和验收阶段，{measure}由责任部门归档。",
        doc_index,
        _dense_rank(difficulty),
        parent_id=f"{case_id}-target-parent",
        child_start_char=400,
        child_end_char=720,
        content_kind="clause",
        clause_id="1.2",
        section="适用范围",
    )
    query = (
        f"文号{number}中关于{topic}的适用范围是什么？"
        if index % 2
        else f"《{title}》对{topic}和{measure}如何规定？"
    )
    return _case(
        case_id,
        "document_reference",
        difficulty,
        query,
        [relevant["id"]],
        [
            *_generic_distractors(case_id, index, 1, 5),
            relevant,
            *_generic_distractors(case_id, index + 5, 6, 4),
        ],
    )


def _overlap_case(case_id: str, index: int, count: int) -> dict:
    difficulty = _difficulty(index, count)
    topic, measure = TOPICS[index % len(TOPICS)]
    first_doc = index % len(DOCUMENTS)
    second_doc = (index + 5) % len(DOCUMENTS)
    primary_text = f"{topic}采用分级控制，{measure}由项目办公室按月核验并形成闭环记录。"
    primary = _candidate(
        case_id,
        "primary",
        primary_text,
        first_doc,
        _dense_rank(difficulty),
        parent_id=f"{case_id}-overlap-parent",
        child_start_char=0,
        child_end_char=360,
        content_kind="paragraph",
        section=f"{topic}控制",
    )
    duplicate_one = _candidate(
        case_id,
        "overlap-1",
        primary_text + "下一段说明整改流程。",
        first_doc,
        1,
        parent_id=f"{case_id}-overlap-parent",
        child_start_char=120,
        child_end_char=480,
        content_kind="paragraph",
        section=f"{topic}控制",
    )
    duplicate_two = _candidate(
        case_id,
        "overlap-2",
        "上一段说明控制原则。" + primary_text,
        first_doc,
        2,
        parent_id=f"{case_id}-overlap-parent",
        child_start_char=240,
        child_end_char=600,
        content_kind="paragraph",
        section=f"{topic}控制",
    )
    secondary = _candidate(
        case_id,
        "secondary",
        f"另一方案针对{topic}采用动态授权，并将{measure}纳入季度验收清单。",
        second_doc,
        5 if difficulty == "hard" else 3,
        parent_id=f"{case_id}-secondary-parent",
        child_start_char=900,
        child_end_char=1220,
        content_kind="paragraph",
        section=f"{topic}验收",
    )
    return _case(
        case_id,
        "overlap_dedup",
        difficulty,
        f"请比较两个方案在{topic}和{measure}上的做法，分别给出不同来源的证据。",
        [primary["id"], secondary["id"]],
        [
            duplicate_one,
            duplicate_two,
            primary,
            secondary,
            *_generic_distractors(case_id, index, 4, 6),
        ],
        expected_groups=[
            [primary["id"], duplicate_one["id"], duplicate_two["id"]],
            [secondary["id"]],
        ],
    )


def _cross_source_case(case_id: str, index: int, count: int) -> dict:
    difficulty = _difficulty(index, count)
    first_doc = index % len(DOCUMENTS)
    second_doc = (index + 1) % len(DOCUMENTS)
    first_title = DOCUMENTS[first_doc][0]
    second_title = DOCUMENTS[second_doc][0]
    topic, measure = TOPICS[index % len(TOPICS)]
    first = _candidate(
        case_id,
        "source-a",
        f"{first_title}要求{topic}采用集中管理，重点检查{measure}。",
        first_doc,
        _dense_rank(difficulty),
        parent_id=f"{case_id}-a-parent",
        child_start_char=200,
        child_end_char=500,
        content_kind="clause",
        clause_id="3.1",
        section=f"{topic}要求",
    )
    second = _candidate(
        case_id,
        "source-b",
        f"{second_title}要求{topic}采用分级管理，并将{measure}列入验收。",
        second_doc,
        6 if difficulty == "hard" else 3,
        parent_id=f"{case_id}-b-parent",
        child_start_char=700,
        child_end_char=1010,
        content_kind="clause",
        clause_id="4.2",
        section=f"{topic}要求",
    )
    duplicate = _candidate(
        case_id,
        "source-a-duplicate",
        f"{first_title}再次说明{topic}集中管理及{measure}检查要求。",
        first_doc,
        1,
        parent_id=f"{case_id}-a-duplicate-parent",
        child_start_char=1100,
        child_end_char=1400,
        content_kind="paragraph",
        section=f"{topic}补充说明",
    )
    return _case(
        case_id,
        "cross_source_recall",
        difficulty,
        f"对比《{first_title}》和《{second_title}》在{topic}及{measure}上的规定，分别说明差异。",
        [first["id"], second["id"]],
        [duplicate, first, second, *_generic_distractors(case_id, index, 2, 7)],
        expected_groups=[
            [first["id"], duplicate["id"]],
            [second["id"]],
        ],
    )


def _broad_summary_case(case_id: str, index: int, count: int) -> dict:
    difficulty = _difficulty(index, count)
    doc_index = index % len(DOCUMENTS)
    title = DOCUMENTS[doc_index][0]
    topic, measure = TOPICS[index % len(TOPICS)]
    overview = _candidate(
        case_id,
        "overview",
        f"## 总体要求\n{title}围绕{topic}明确总体原则、责任分工和实施边界。",
        doc_index,
        _dense_rank(difficulty),
        parent_id=f"{case_id}-overview-parent",
        child_start_char=0,
        child_end_char=360,
        content_kind="paragraph",
        section_path=["总体要求"],
        section="总体要求",
    )
    implementation = _candidate(
        case_id,
        "implementation",
        f"## 实施与验收\n项目分阶段实施{topic}，最终依据{measure}完成验收和持续改进。",
        doc_index,
        5 if difficulty == "hard" else 2,
        parent_id=f"{case_id}-implementation-parent",
        child_start_char=1600,
        child_end_char=1980,
        content_kind="paragraph",
        section_path=["实施与验收"],
        section="实施与验收",
    )
    narrow = _candidate(
        case_id,
        "narrow-negative",
        f"{topic}术语定义：本术语仅用于附录，不代表总体实施或{measure}要求。",
        doc_index,
        1,
        parent_id=f"{case_id}-narrow-parent",
        child_start_char=600,
        child_end_char=900,
        content_kind="paragraph",
        section_path=["术语"],
        section="术语",
    )
    return _case(
        case_id,
        "broad_summary",
        difficulty,
        f"请概述《{title}》关于{topic}的总体要求、实施步骤和{measure}验收要点。",
        [overview["id"], implementation["id"]],
        [narrow, overview, implementation, *_generic_distractors(case_id, index, 2, 7)],
    )


if __name__ == "__main__":
    raise SystemExit(main())
