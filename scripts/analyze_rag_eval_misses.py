import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import eval_rag
from core.rag_defaults import DEFAULT_RAG_TOP_K


DEFAULT_DATASET = PROJECT_ROOT / "eval" / "retrieval_quality_blind_v0.3.jsonl"
DEFAULT_RESULTS = PROJECT_ROOT / "eval" / "results" / "retrieval_baseline_v0.3.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze missed sources in a saved SoulDrive retrieval evaluation.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="Evaluation dataset JSONL path.")
    parser.add_argument("--results", default=str(DEFAULT_RESULTS), help="Saved retrieval results JSONL path.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_RAG_TOP_K, help="Evidence depth to inspect.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum sample misses to print.")
    parser.add_argument("--json-output", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    report = analyze_result_files(Path(args.dataset), Path(args.results), top_k=args.top_k)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print_human_report(report, limit=args.limit)
    return 0


def analyze_result_files(dataset_path: Path, results_path: Path, top_k: int = DEFAULT_RAG_TOP_K) -> dict[str, Any]:
    records = eval_rag.load_dataset(dataset_path)
    results = eval_rag.load_results(results_path, records)
    results_by_id = {result.id: result for result in results}
    records_by_id = {record.id: record for record in records}

    misses = []
    misses_by_type: Counter[str] = Counter()
    missed_sources: Counter[str] = Counter()
    partial_multi_doc_misses = []

    for record in records:
        if not record.expected_sources:
            continue
        result = results_by_id[record.id]
        evidence = result.evidence[:top_k]
        hit_sources = [
            source
            for index, source in enumerate(record.expected_sources)
            if any(
                eval_rag.evidence_matches_expected_source(
                    item,
                    source,
                    record.expected_document_ids[index]
                    if record.expected_document_ids
                    else None,
                )
                for item in evidence
            )
        ]
        missing_sources = [source for source in record.expected_sources if source not in hit_sources]
        if not missing_sources:
            continue

        sample = {
            "id": record.id,
            "type": record.type,
            "question": record.question,
            "expected_sources": record.expected_sources,
            "expected_document_ids": record.expected_document_ids,
            "document_identity_required": bool(record.expected_document_ids),
            "hit_sources": hit_sources,
            "missed_sources": missing_sources,
            "top_sources": [str(item.get("source_filename") or "") for item in evidence],
            "top_document_ids": [eval_rag.evidence_document_id(item) for item in evidence],
        }
        misses.append(sample)
        misses_by_type[record.type] += 1
        for source in missing_sources:
            missed_sources[source] += 1
        if record.requires_multi_doc and hit_sources:
            partial_multi_doc_misses.append(sample)

    return {
        "dataset": str(dataset_path),
        "results": str(results_path),
        "top_k": top_k,
        "sample_count": len(records),
        "positive_sample_count": sum(1 for record in records if record.expected_sources),
        "miss_count": len(misses),
        "miss_rate": len(misses) / max(sum(1 for record in records if record.expected_sources), 1),
        "misses_by_type": dict(sorted(misses_by_type.items())),
        "missed_sources": dict(sorted(missed_sources.items(), key=lambda item: (-item[1], item[0]))),
        "partial_multi_doc_misses": partial_multi_doc_misses,
        "miss_samples": misses,
        "missing_result_ids": sorted(record_id for record_id in records_by_id if record_id not in results_by_id),
    }


def print_human_report(report: dict[str, Any], limit: int = 20) -> None:
    print("RAG retrieval miss analysis")
    print(f"Dataset: {report['dataset']}")
    print(f"Results: {report['results']}")
    print(f"Samples: {report['sample_count']}")
    print(f"Positive samples: {report['positive_sample_count']}")
    print(f"Misses@{report['top_k']}: {report['miss_count']} ({report['miss_rate'] * 100:.1f}%)")

    print("\nMisses by type:")
    for sample_type, count in report["misses_by_type"].items():
        print(f"- {sample_type}: {count}")

    print("\nMissed expected sources:")
    for source, count in report["missed_sources"].items():
        print(f"- {source}: {count}")

    print("\nPartial multi-document misses:")
    for sample in report["partial_multi_doc_misses"][:limit]:
        print(
            f"- {sample['id']}: hit={', '.join(sample['hit_sources']) or 'none'}; "
            f"missed={', '.join(sample['missed_sources'])}"
        )

    print("\nSample misses:")
    for sample in report["miss_samples"][:limit]:
        print(
            f"- {sample['id']} [{sample['type']}]: expected={', '.join(sample['expected_sources'])}; "
            f"missed={', '.join(sample['missed_sources'])}; top={', '.join(sample['top_sources']) or 'none'}"
        )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
