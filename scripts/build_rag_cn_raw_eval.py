"""Build a document-disjoint RAG evaluation set from the original RAG PDFs."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

import fitz


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = PROJECT_ROOT / "runtime" / "workspace" / "data" / "source_materials" / "rag_cn"
DEFAULT_OUTPUT = PROJECT_ROOT / "eval" / "fixtures" / "rag_cn_raw_v0.1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a document-disjoint RAG set from original RAG PDFs.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    source = args.source.resolve()
    output = args.output.resolve()
    if not source.is_dir():
        parser.error(f"source directory does not exist: {source}")
    if output == source or source in output.parents:
        parser.error("evaluation output must stay outside the source directory")

    documents = [_inspect_pdf(path) for path in sorted(source.glob("*.pdf"))]
    if not documents:
        parser.error(f"no PDFs found: {source}")
    split_map = _document_split(documents)
    records = [_build_records(document, split_map[document["doc_id"]]) for document in documents]
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "rag_cn_raw_eval_v0.1",
                "source_directory": str(source),
                "source_files_are_raw": True,
                "split_unit": "document",
                "sealed_labels_must_not_enter_runtime": True,
                "documents": documents,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    for split in ("dev", "sealed"):
        rows = [record for record in records for record in record["queries"] if record["split"] == split]
        (output / f"{split}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
            encoding="utf-8",
        )
    summary = {
        "documents": len(documents),
        "dev_documents": sum(split == "dev" for split in split_map.values()),
        "sealed_documents": sum(split == "sealed" for split in split_map.values()),
        "dev_queries": sum(record["split"] == "dev" for record in records for _ in record["queries"]),
        "sealed_queries": sum(record["split"] == "sealed" for record in records for _ in record["queries"]),
        "output": str(output),
    }
    (output / "dataset_card.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _inspect_pdf(path: Path) -> dict:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    document = fitz.open(path)
    text = "\n".join(page.get_text() for page in document)
    headings = _headings(text)
    return {
        "doc_id": f"RAGCN-{digest[:12]}",
        "filename": path.name,
        "sha256": digest,
        "page_count": len(document),
        "text_chars": len(text),
        "headings": headings[:12],
    }


def _document_split(documents: list[dict]) -> dict[str, str]:
    ordered = sorted(documents, key=lambda item: item["sha256"])
    sealed_count = max(1, len(ordered) // 3)
    return {document["doc_id"]: ("sealed" if index < sealed_count else "dev") for index, document in enumerate(ordered)}


def _build_records(document: dict, split: str) -> dict:
    title = Path(document["filename"]).stem
    headings = document["headings"] or [title]
    topics = [heading for heading in headings if not _looks_like_title(heading)][:3] or headings[:1]
    queries = []
    templates = (
        ("known_title", f"请概括《{title}》围绕“{{topic}}”的核心论述，并给出页码证据。"),
        ("blind_topic", "请根据当前本地资料，说明关于“{topic}”的主要观点和适用边界，并给出证据。"),
        ("evidence_lookup", "请定位资料中关于“{topic}”的关键原文证据，说明它支持什么结论。"),
        ("structure_summary", "请梳理资料中“{topic}”对应的背景、方法或结论结构，不要补充资料外事实。"),
    )
    for index, (kind, template) in enumerate(templates):
        topic = topics[index % len(topics)]
        queries.append(
            {
                "id": f"{document['doc_id']}-{index + 1:02d}",
                "split": split,
                "type": kind,
                "question": template.format(topic=topic),
                "expected_sources": [title],
                "expected_keywords": _keywords(topic),
                "should_refuse": False,
                "source_doc_id": document["doc_id"],
                "source_sha256": document["sha256"],
            }
        )
    return {"split": split, "queries": queries}


def _headings(text: str) -> list[str]:
    result = []
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" -\t")
        # PyMuPDF exposes missing glyphs as replacement characters.  Such a
        # heading is not a stable, answerable evaluation target.
        if "�" in line or "?" in line and line.count("?") >= 2:
            continue
        if not 4 <= len(line) <= 48 or re.fullmatch(r"[\d.]+", line):
            continue
        if line.endswith(("。", ".", ",", "，", ";", "；")):
            continue
        if re.match(r"^(第?[一二三四五六七八九十百\d]+[章节、.]|摘要|引言|结论|参考文献|Abstract)", line, re.I):
            if line not in result:
                result.append(line)
    return result


def _looks_like_title(value: str) -> bool:
    return value.casefold() in {"摘要", "abstract", "引言", "结论", "参考文献"}


def _keywords(value: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}|[\u4e00-\u9fff]{2,6}", value)
    return list(dict.fromkeys(words))[:6]


if __name__ == "__main__":
    raise SystemExit(main())
