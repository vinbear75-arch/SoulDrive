"""Build the ingestion-quality evaluation dataset from the corpus manifest.

The ingestion set is intentionally separate from retrieval questions. It checks
whether document parsing, chunking, metadata, and index writing preserve the
structures that enterprise plans, project proposals, policy documents, standards,
and RAG research papers need for downstream retrieval.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "eval" / "corpus_manifest_v0.2.json"
INVENTORY_PATH = PROJECT_ROOT / "eval" / "corpus_file_inventory_v0.2.json"
MODALITY_GROUND_TRUTH_PATH = PROJECT_ROOT / "eval" / "corpus_modality_ground_truth_v0.1.json"
DATASET_PATH = PROJECT_ROOT / "eval" / "ingestion_quality_v0.1.jsonl"


def main() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    modality_ground_truth = json.loads(
        MODALITY_GROUND_TRUTH_PATH.read_text(encoding="utf-8")
    )
    inventory_by_doc_id = {item["doc_id"]: item for item in inventory["files"]}
    modality_by_doc_id = {
        item["doc_id"]: item
        for item in modality_ground_truth["documents"]
    }
    records: list[dict[str, Any]] = []

    for document in manifest["documents"]:
        file_info = inventory_by_doc_id.get(document["doc_id"], {})
        modality_info = modality_by_doc_id.get(document["doc_id"], {})
        records.extend(build_records(document, file_info, modality_info))

    DATASET_PATH.write_text(
        "\n".join(
            json.dumps({**record, "id": f"ING-{index:03d}"}, ensure_ascii=False, separators=(",", ":"))
            for index, record in enumerate(records, start=1)
        )
        + "\n",
        encoding="utf-8",
    )


def build_records(
    document: dict[str, Any],
    file_info: dict[str, Any],
    modality_info: dict[str, Any],
) -> list[dict[str, Any]]:
    doc_id = str(document["doc_id"])
    filename = str(document["local_filename"])
    tags = set(document.get("topic_tags", []))
    language = str(document.get("language") or "zh")
    document_class = classify_document(tags, language)
    min_chunk_count = min_chunk_count_for(file_info)
    base_focus = focus_for(document)
    required_modalities = ground_truth_modalities(modality_info)

    structure_metadata = [
        "page",
        "section",
        "dynamic_chunk",
        "paragraph_overlap",
        "document_context",
        "token_safe",
        "formal_window",
    ]
    if document_class == "scenario_list":
        structure_metadata = [
            "page",
            "section",
            "dynamic_chunk",
            "paragraph_overlap",
            "document_context",
            "token_safe",
        ]

    structure_record = {
        "doc_id": doc_id,
        "source_filename": filename,
        "language": language,
        "document_class": document_class,
        "evaluation_scope": "structure_and_sectioning",
        "difficulty": "hard" if min_chunk_count >= 10 else "medium",
        "min_chunk_count": min_chunk_count,
        "required_metadata": structure_metadata,
        "required_modalities": required_modalities,
        "focus": unique([*base_focus, "章节层级", "页码追溯", "段落重叠"]),
    }

    overlap_record = {
        "doc_id": doc_id,
        "source_filename": filename,
        "language": language,
        "document_class": document_class,
        "evaluation_scope": "paragraph_window_and_overlap",
        "difficulty": "hard" if min_chunk_count >= 8 else "medium",
        "min_chunk_count": min_chunk_count,
        "required_metadata": [
            "page",
            "dynamic_chunk",
            "paragraph_overlap",
            "formal_window",
            "token_safe",
        ],
        "required_modalities": [],
        "focus": unique([*base_focus, "两段滑窗", "单段重叠", "边界连续性"]),
    }

    context_record = {
        "doc_id": doc_id,
        "source_filename": filename,
        "language": language,
        "document_class": document_class,
        "evaluation_scope": "context_and_provenance",
        "difficulty": "hard",
        "min_chunk_count": min_chunk_count,
        "required_metadata": [
            "page",
            "document_context",
            "retrieval_context",
            "source_provenance",
            "token_safe",
        ],
        "required_modalities": [],
        "focus": unique([*base_focus, "文档标题上下文", "来源溯源", "章节语境"]),
    }

    index_record = {
        "doc_id": doc_id,
        "source_filename": filename,
        "language": language,
        "document_class": document_class,
        "evaluation_scope": "index_integrity",
        "difficulty": "hard",
        "min_chunk_count": min_chunk_count,
        "required_metadata": [
            "page",
            "parent_linkage",
            "semantic_units",
            "index_schema",
            "token_safe",
        ],
        "required_modalities": [],
        "focus": unique([*base_focus, "父子索引", "稳定标识", "模型版本"]),
    }

    modality_record = {
        "doc_id": doc_id,
        "source_filename": filename,
        "language": language,
        "document_class": document_class,
        "evaluation_scope": modality_scope(required_modalities, tags, language),
        "difficulty": modality_difficulty(tags, file_info),
        "min_chunk_count": max(3, min_chunk_count - 2),
        "required_metadata": [
            "page",
            "dynamic_chunk",
            "paragraph_overlap",
            "document_context",
            "token_safe",
        ],
        "required_modalities": required_modalities,
        "focus": unique([*base_focus, *modality_focus(required_modalities, tags, language)]),
    }

    content_record = {
        "doc_id": doc_id,
        "source_filename": filename,
        "language": language,
        "document_class": document_class,
        "evaluation_scope": "content_integrity",
        "difficulty": "medium",
        "min_chunk_count": min_chunk_count,
        "required_metadata": [
            "page",
            "content_integrity",
            "dynamic_chunk",
            "token_safe",
        ],
        "required_modalities": [],
        "focus": unique([*base_focus, "控制字符清洗", "有效正文", "编码完整性"]),
    }

    return [
        structure_record,
        overlap_record,
        context_record,
        index_record,
        modality_record,
        content_record,
    ]


def classify_document(tags: set[str], language: str) -> str:
    if language == "en":
        return "rag_research_reference"
    if "industry_scenario" in tags:
        return "scenario_list"
    if "standard" in tags or "data_classification" in tags or "privacy" in tags:
        return "government_standard"
    if "governance" in tags or "ai_safety" in tags or "data_elements" in tags:
        return "government_formal_document"
    return "enterprise_technical_plan"


def min_chunk_count_for(file_info: dict[str, Any]) -> int:
    pages = int(file_info.get("page_count") or 0)
    text_chars = int(file_info.get("text_chars") or 0)
    by_pages = max(3, pages // 5)
    by_text = max(3, text_chars // 8000)
    return min(18, max(by_pages, by_text))


def focus_for(document: dict[str, Any]) -> list[str]:
    tags = list(document.get("topic_tags", []))
    title = str(document.get("title") or "")
    focus = [tag.replace("_", " ") for tag in tags[:4]]
    if "RAG" in title or "Retrieval" in title:
        focus.append("RAG")
    if "安全" in title or "治理" in title:
        focus.append("安全治理")
    if "数据" in title:
        focus.append("数据治理")
    if "部署" in title or "落地" in title:
        focus.append("项目方案")
    return focus


def ground_truth_modalities(modality_info: dict[str, Any]) -> list[str]:
    available = {
        str(item)
        for item in modality_info.get("required_modalities", [])
        if str(item) in {"table", "image"}
    }
    return [item for item in ("table", "image") if item in available]


def modality_scope(modalities: list[str], tags: set[str], language: str) -> str:
    modality_set = set(modalities)
    if not modality_set:
        return "text_only_layout"
    if language == "en" and modality_set == {"table", "image"}:
        return "english_research_layout"
    if modality_set == {"table"} or "standard" in tags or "data_security" in tags or "privacy" in tags:
        return "table_and_clause_preservation"
    if "industry_scenario" in tags and "table" in modality_set:
        return "scenario_list_preservation"
    if "image" in modality_set and ("rag" in tags or "private_deployment" in tags):
        return "architecture_figure_metadata"
    return "formal_report_layout"


def modality_difficulty(tags: set[str], file_info: dict[str, Any]) -> str:
    pages = int(file_info.get("page_count") or 0)
    if pages >= 40 or "standard" in tags or "rag" in tags:
        return "hard"
    return "medium"


def modality_focus(modalities: list[str], tags: set[str], language: str) -> list[str]:
    modality_set = set(modalities)
    focus = ["表格保留", "图示元数据"] if language == "en" and modality_set else []
    if "table" in modality_set and ("standard" in tags or "data_security" in tags):
        focus.extend(["条款结构", "表格保留"])
    if "image" in modality_set and ("rag" in tags or "private_deployment" in tags):
        focus.extend(["架构图", "流程图"])
    if "table" in modality_set and "industry_scenario" in tags:
        focus.extend(["场景清单", "项目条目"])
    if not focus:
        focus.extend(["报告结构", "正文版式"])
    return focus


def unique(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


if __name__ == "__main__":
    main()
