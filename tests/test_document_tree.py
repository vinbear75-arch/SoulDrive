from types import SimpleNamespace

from core.document_tree import SECTION_TREE_VERSION, build_section_tree, section_node_id
from core.knowledge_base import LocalKnowledgeBase
from core.retrieval import _section_route_match


def _chunk(path, content):
    return SimpleNamespace(
        metadata={"section_path": path},
        page_content=content,
    )


def test_section_tree_persists_stable_parent_child_metadata_and_summary():
    chunks = [
        _chunk(["总览", "检索"], "混合检索负责召回候选证据。后续说明重排。"),
        _chunk(["总览", "生成"], "生成阶段只使用通过证据门的内容。"),
    ]

    tree = build_section_tree("paper-1", chunks)
    nodes = {node["path_key"]: node for node in tree["nodes"]}
    root = nodes["总览"]
    retrieval = nodes["总览 > 检索"]

    assert tree["version"] == SECTION_TREE_VERSION
    assert root["child_node_ids"] == [retrieval["node_id"], nodes["总览 > 生成"]["node_id"]]
    assert retrieval["parent_node_id"] == root["node_id"]
    assert retrieval["summary"] == "混合检索负责召回候选证据。"
    assert tree["by_parent_index"][0]["section_node_id"] == retrieval["node_id"]
    assert tree["by_parent_index"][0]["section_parent_node_id"] == root["node_id"]
    assert tree["by_parent_index"][0]["section_ancestor_node_ids"] == root["node_id"]
    assert section_node_id("paper-1", ["总览", "检索"]) == retrieval["node_id"]


def test_section_route_traverses_persisted_ancestor_nodes():
    root_id = section_node_id("paper-1", ["总览"])
    retrieval_id = section_node_id("paper-1", ["总览", "检索"])
    sparse_id = section_node_id("paper-1", ["总览", "检索", "稀疏检索"])
    tree_nodes = {
        root_id: {"title": "总览", "path_key": "总览", "depth": 1},
        retrieval_id: {"title": "检索", "path_key": "总览 > 检索", "depth": 2},
        sparse_id: {"title": "稀疏检索", "path_key": "总览 > 检索 > 稀疏检索", "depth": 3},
    }

    route = _section_route_match(
        "请定位“检索”的原文",
        {
            "section_node_id": sparse_id,
            "section_ancestor_node_ids": f"{root_id}|{retrieval_id}",
        },
        "正文",
        tree_nodes=tree_nodes,
    )

    assert route["mode"] == "descendant"
    assert route["source"] == "persisted_section_tree_v1"
    assert route["node_paths"] == ("总览 > 检索",)
    assert route["max_node_depth"] == 2


def test_section_route_accepts_a_full_persisted_path_reference():
    root_id = section_node_id("paper-1", ["总览"])
    retrieval_id = section_node_id("paper-1", ["总览", "检索"])
    tree_nodes = {
        root_id: {"title": "总览", "path_key": "总览", "depth": 1},
        retrieval_id: {"title": "检索", "path_key": "总览 > 检索", "depth": 2},
    }

    route = _section_route_match(
        "请定位“总览 > 检索”的原文",
        {
            "section_node_id": retrieval_id,
            "section_ancestor_node_ids": root_id,
        },
        tree_nodes=tree_nodes,
    )

    assert route["mode"] == "exact_node"
    assert route["node_paths"] == ("总览 > 检索",)


def test_section_candidates_uses_persisted_tree_when_available():
    kb = LocalKnowledgeBase.__new__(LocalKnowledgeBase)
    root_id = section_node_id("paper-1", ["总览"])
    retrieval_id = section_node_id("paper-1", ["总览", "检索"])
    kb._source_candidate_cache = [
        (
            "child-1",
            "检索正文",
            {
                "source_filename": "paper.pdf",
                "section_path": ["总览", "检索"],
                "section_node_id": retrieval_id,
                "section_ancestor_node_ids": root_id,
                "section_node_title": "检索",
                "section_path_key": "总览 > 检索",
                "section_node_depth": 2,
            },
        )
    ]

    candidates = kb._section_candidates("请定位“检索”的原文", limit=1)

    assert candidates[0].metadata["_section_route_source"] == "persisted_section_tree_v1"
