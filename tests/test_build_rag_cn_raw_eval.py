import json
from pathlib import Path

from scripts.build_rag_cn_raw_eval import _document_split, _headings


def test_headings_ignore_body_sentences_and_keep_numbered_sections():
    assert _headings("1. 方法\n这是正文。\n第2章 检索增强生成\n参考文献\n") == ["1. 方法", "第2章 检索增强生成", "参考文献"]


def test_document_split_is_disjoint_and_deterministic():
    documents = [{"doc_id": f"doc-{index}", "sha256": str(index)} for index in range(6)]
    first = _document_split(documents)
    second = _document_split(list(reversed(documents)))
    assert first == second
    assert set(first.values()) == {"dev", "sealed"}


def test_generated_fixture_is_outside_runtime_workspace():
    fixture = Path("eval/fixtures/rag_cn_raw_v0.1")
    if not (fixture / "manifest.json").exists():
        return
    manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
    source = Path(manifest["source_directory"]).resolve()
    assert fixture.resolve() != source
    assert source not in fixture.resolve().parents
