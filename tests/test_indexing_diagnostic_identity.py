import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core import mcp_server, runtime_state
from core.indexer import DriveIndexer
from core.parent_doc_store import ParentDocumentStore
from core.project_catalog import ProjectCatalog
from core.secure_document_store import SecureDocumentStore
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import derive_workspace_keys


@pytest.fixture
def same_named_documents(tmp_path, monkeypatch):
    workspace = SoulDriveWorkspace(str(tmp_path / "workspace")).ensure()
    keys = derive_workspace_keys(b"d" * 32)
    set_workspace_keys(workspace.root_path, keys)
    catalog = ProjectCatalog(workspace)
    store = SecureDocumentStore(workspace, keys)
    try:
        for suffix in ("a", "b"):
            project_id = f"project-{suffix}"
            content = f"payload-{suffix}".encode("utf-8")
            catalog.create_project(project_id, project_id=project_id)
            catalog.register_imported_document(
                project_id, "same", hashlib.md5(content).hexdigest(), "same.txt"
            )
            store.import_document_bytes("same.txt", content)
        entries = list(store.iter_documents())
        ids = {
            store.read_document_bytes(item["object_id"]).decode("utf-8")[-1]: item["object_id"]
            for item in entries
        }
    finally:
        store.close()
        catalog.close()
    monkeypatch.setattr(runtime_state, "STATE_PATH", str(tmp_path / "runtime_state.json"))
    monkeypatch.setattr(mcp_server, "_indexed_source_filenames", lambda _: {"same.txt"})
    monkeypatch.setattr(mcp_server, "_indexed_document_ids", lambda _: {f"secure_{value}" for value in ids.values()})
    monkeypatch.setattr(mcp_server, "_indexed_source_hashes", lambda _: {})
    try:
        yield workspace, ids, entries
    finally:
        clear_workspace_keys(workspace.root_path)


@pytest.mark.parametrize("status", ["indexed", "failed", "processing"])
def test_same_named_project_diagnostics_bind_document_identity(same_named_documents, monkeypatch, status):
    workspace, ids, _ = same_named_documents
    observations = [
        {"source_filename": "same.txt", "document_id": f"secure_{ids[suffix]}",
         "project_id": f"project-{suffix}", "status": "indexed", "elapsed_ms": elapsed}
        for suffix, elapsed in (("a", 11), ("b", 23))
    ]
    state = {"indexing": {"status": "completed", "observability": {"documents": observations}}}
    if status == "failed":
        state["indexing"]["failures"] = [{
            "source_filename": "same.txt", "document_id": f"secure_{ids['b']}",
            "project_id": "project-b", "error_code": "NO_PARSEABLE_CHUNKS", "category": "parser",
        }]
    elif status == "processing":
        state["indexing"].update(status="indexing", current_file="same.txt",
                                 current_document_id=f"secure_{ids['b']}", current_project_id="project-b")
    monkeypatch.setattr(mcp_server, "get_runtime_state", lambda: state)
    documents = {item["project_id"]: item for item in mcp_server._document_library_payload(workspace)["documents"]}

    assert documents["project-a"]["index_status"] == "indexed"
    assert documents["project-a"]["index_observation"]["elapsed_ms"] == 11
    assert documents["project-a"]["index_failure"] is None
    assert documents["project-b"]["index_status"] == status
    assert documents["project-b"]["index_observation"]["elapsed_ms"] == 23
    assert bool(documents["project-b"]["index_failure"]) is (status == "failed")


@pytest.mark.parametrize("ambiguity", ["library", "observations"])
def test_legacy_same_name_diagnostics_remain_unknown(same_named_documents, monkeypatch, ambiguity):
    workspace, ids, entries = same_named_documents
    if ambiguity == "observations":
        selected = next(item for item in entries if item["object_id"] == ids["a"])
        monkeypatch.setattr(SecureDocumentStore, "iter_documents", lambda _: [selected])
    state = {"indexing": {
        "status": "indexing", "current_file": "same.txt",
        "failures": [{"source_filename": "same.txt", "error_code": "NO_PARSEABLE_CHUNKS"}],
        "observability": {"documents": [
            {"source_filename": "same.txt", "status": "indexed", "elapsed_ms": 11},
        ] + ([{"source_filename": "same.txt", "status": "failed", "elapsed_ms": 23}]
             if ambiguity == "observations" else [])},
    }}
    monkeypatch.setattr(mcp_server, "get_runtime_state", lambda: state)
    documents = mcp_server._document_library_payload(workspace)["documents"]
    assert len(documents) == (2 if ambiguity == "library" else 1)
    assert all(item["index_status"] == "indexed" for item in documents)
    assert all(item["index_observation"] is None and item["index_failure"] is None for item in documents)


@pytest.mark.parametrize("mismatch", ["document", "project"])
def test_explicit_diagnostic_identity_never_falls_back_to_unique_name(same_named_documents, monkeypatch, mismatch):
    workspace, ids, entries = same_named_documents
    selected = next(item for item in entries if item["object_id"] == ids["a"])
    monkeypatch.setattr(SecureDocumentStore, "iter_documents", lambda _: [selected])
    state = {"indexing": {"status": "completed", "observability": {"documents": [{
        "source_filename": "same.txt", "status": "indexed", "elapsed_ms": 999,
        "document_id": f"secure_{ids['b' if mismatch == 'document' else 'a']}",
        "project_id": "project-b",
    }]}}}
    monkeypatch.setattr(mcp_server, "get_runtime_state", lambda: state)
    documents = mcp_server._document_library_payload(workspace)["documents"]
    assert len(documents) == 1 and documents[0]["project_id"] == "project-a"
    assert documents[0]["index_observation"] is None


def test_plain_same_named_catalog_documents_use_their_exact_identity(tmp_path, monkeypatch):
    workspace = SoulDriveWorkspace(str(tmp_path / "workspace")).ensure()
    catalog = ProjectCatalog(workspace)
    observations = []
    try:
        for suffix, elapsed in (("a", 11), ("b", 23)):
            relative_path = f"{suffix}/same.txt"
            source = Path(workspace.papers_path) / relative_path
            source.parent.mkdir()
            source.write_text(f"payload-{suffix}", encoding="utf-8")
            project_id, asset_id = f"project-{suffix}", f"asset-{suffix}"
            catalog.create_project(project_id, project_id=project_id)
            catalog.create_asset(project_id, suffix, "document", asset_id=asset_id)
            catalog.create_asset_version(asset_id, mcp_server.file_md5(source), relative_path)
            observations.append({"source_filename": "same.txt", "document_id": asset_id,
                                 "project_id": project_id, "status": "indexed", "elapsed_ms": elapsed})
    finally:
        catalog.close()
    monkeypatch.setattr(mcp_server, "get_runtime_state", lambda: {
        "indexing": {"status": "completed", "observability": {"documents": observations}}
    })
    monkeypatch.setattr(mcp_server, "_indexed_source_filenames", lambda _: {"same.txt"})
    documents = {item["relative_path"]: item for item in mcp_server._document_library_payload(workspace)["documents"]}
    assert documents["a/same.txt"]["project_id"] == "project-a"
    assert documents["a/same.txt"]["index_observation"]["elapsed_ms"] == 11
    assert documents["b/same.txt"]["project_id"] == "project-b"
    assert documents["b/same.txt"]["index_observation"]["elapsed_ms"] == 23


def test_indexer_emits_identity_for_success_failure_progress_and_public_failure(same_named_documents):
    workspace, ids, _ = same_named_documents
    indexer = DriveIndexer()
    parser = Mock()
    parser.last_parse_stats = {}

    def parse(path):
        if Path(path).read_bytes() == b"payload-b":
            raise ValueError("synthetic parser failure")
        return [SimpleNamespace(page_content="synthetic parsed text", metadata={})]

    parser.parse_and_chunk.side_effect = parse
    kb = Mock()
    kb.collection.get.return_value = {"ids": [], "metadatas": []}
    kb.ingest_chunks.return_value = {}
    with patch("core.knowledge_base.LocalKnowledgeBase", return_value=kb), patch.object(
        indexer, "_parser_for_path", return_value=parser
    ), patch("core.indexer.update_indexing_status", wraps=runtime_state.update_indexing_status) as updates:
        try:
            indexer.sync_workspace(workspace, auth_level="BASIC")
            state = runtime_state.get_runtime_state()["indexing"]
        finally:
            indexer.close()
    observations = {item["document_id"]: item for item in state["observability"]["documents"]}
    assert observations[f"secure_{ids['a']}"]["project_id"] == "project-a"
    assert observations[f"secure_{ids['b']}"]["project_id"] == "project-b"
    failure = state["failures"][0]
    assert failure["document_id"] == f"secure_{ids['b']}" and failure["project_id"] == "project-b"
    progress = [call.kwargs for call in updates.call_args_list if call.kwargs.get("current_file")]
    assert {item["current_document_id"] for item in progress} == {f"secure_{value}" for value in ids.values()}
    assert {item["current_project_id"] for item in progress} == {"project-a", "project-b"}
    assert state["current_document_id"] is None and state["current_project_id"] is None
    public_failure = mcp_server._public_indexing_failure(failure)
    assert public_failure["document_id"] == failure["document_id"]
    assert public_failure["project_id"] == failure["project_id"]


def test_unexpected_worker_exit_preserves_current_document_identity(same_named_documents, monkeypatch):
    _, ids, _ = same_named_documents
    process = Mock()
    process.poll.return_value = 17
    monkeypatch.setattr(mcp_server, "indexer_process", process)
    runtime_state.update_indexing_status(
        status="indexing", current_file="same.txt",
        current_document_id=f"secure_{ids['b']}", current_project_id="project-b",
    )
    state = mcp_server.public_runtime_state()["indexing"]
    assert state["status"] == "failed"
    assert state["failures"][0]["document_id"] == f"secure_{ids['b']}"
    assert state["failures"][0]["project_id"] == "project-b"
    assert state["current_document_id"] is None and state["current_project_id"] is None


def test_legacy_progress_update_cannot_keep_previous_document_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_state, "STATE_PATH", str(tmp_path / "runtime_state.json"))
    runtime_state.update_indexing_status(
        status="indexing", current_file="a.txt", current_document_id="document-a", current_project_id="project-a"
    )
    state = runtime_state.update_indexing_status(current_file="b.txt")["indexing"]
    assert state["current_document_id"] is None and state["current_project_id"] is None


@pytest.mark.parametrize("replacement", ["payload-b", "changed-payload"])
def test_same_named_source_change_cannot_borrow_foreign_hash_or_asset(tmp_path, monkeypatch, replacement):
    workspace = SoulDriveWorkspace(str(tmp_path / "workspace")).ensure()
    catalog = ProjectCatalog(workspace)
    parents = ParentDocumentStore(workspace.parent_doc_path)
    try:
        for suffix in ("a", "b"):
            source = Path(workspace.papers_path) / suffix / "same.txt"
            source.parent.mkdir()
            source.write_text(f"payload-{suffix}", encoding="utf-8")
            digest = mcp_server.file_md5(source)
            project_id, asset_id = f"project-{suffix}", f"asset-{suffix}"
            catalog.create_project(project_id, project_id=project_id)
            catalog.create_asset(project_id, suffix, "document", asset_id=asset_id)
            catalog.create_asset_version(asset_id, digest, f"{suffix}/same.txt")
            parents.upsert_parent(suffix, "indexed text", {
                "source_filename": "same.txt", "document_id": asset_id,
                "project_id": project_id, "document_hash": digest,
            })
    finally:
        parents.close()
        catalog.close()
    (Path(workspace.papers_path) / "a/same.txt").write_text(replacement, encoding="utf-8")
    catalog = ProjectCatalog(workspace)
    try:
        binding = catalog.find_asset_version_for_file(
            "a/same.txt", content_hash=mcp_server.file_md5(Path(workspace.papers_path) / "a/same.txt")
        )
        assert binding is None, "a registered exact path must not borrow a different asset by basename"
    finally:
        catalog.close()
    identity_indexer = DriveIndexer()
    identity_indexer.workspace = workspace
    identity_indexer.project_catalog = ProjectCatalog(workspace)
    try:
        identity = identity_indexer._document_identity_for_source(
            str(Path(workspace.papers_path) / "a/same.txt"), "same.txt",
            mcp_server.file_md5(Path(workspace.papers_path) / "a/same.txt"),
        )
        assert identity[:2] == ("asset-a", "project-a")
        assert identity[2] is None, "stable ownership must not pretend the old version matches new bytes"
    finally:
        identity_indexer.close()
    monkeypatch.setattr(mcp_server, "get_runtime_state", lambda: {"indexing": {"status": "idle"}})
    documents = {item["relative_path"]: item for item in mcp_server._document_library_payload(workspace)["documents"]}
    assert documents["a/same.txt"]["source_changed"] is True
    assert documents["a/same.txt"]["index_present"] is True
    assert documents["a/same.txt"]["index_status"] == "changed"
    assert documents["a/same.txt"]["project_id"] == "project-a"
    assert documents["b/same.txt"]["source_changed"] is False


def test_same_named_identical_bytes_have_distinct_ingestion_keys(tmp_path, monkeypatch):
    workspace = SoulDriveWorkspace(str(tmp_path / "workspace")).ensure()
    catalog = ProjectCatalog(workspace)
    try:
        for suffix in ("a", "b"):
            source = Path(workspace.papers_path) / suffix / "same.txt"
            source.parent.mkdir()
            source.write_text("identical payload", encoding="utf-8")
            project_id, asset_id = f"project-{suffix}", f"asset-{suffix}"
            catalog.create_project(project_id, project_id=project_id)
            catalog.create_asset(project_id, suffix, "document", asset_id=asset_id)
            catalog.create_asset_version(asset_id, mcp_server.file_md5(source), f"{suffix}/same.txt")
    finally:
        catalog.close()
    monkeypatch.setattr(runtime_state, "STATE_PATH", str(tmp_path / "runtime_state.json"))
    indexer, parser, kb = DriveIndexer(), Mock(), Mock()
    parser.last_parse_stats = {}
    parser.parse_and_chunk.side_effect = lambda _: [SimpleNamespace(page_content="parsed text", metadata={})]
    kb.collection.get.return_value = {"ids": [], "metadatas": []}
    kb.ingest_chunks.return_value = {}
    with patch("core.knowledge_base.LocalKnowledgeBase", return_value=kb), patch.object(
        indexer, "_parser_for_path", return_value=parser
    ):
        try:
            indexer.sync_workspace(workspace, auth_level="BASIC")
        finally:
            indexer.close()
    ingestions = kb.ingest_chunks.call_args_list
    assert len(ingestions) == 2
    assert len({call.args[0] for call in ingestions}) == 2
    assert {call.args[1][0].metadata["document_id"] for call in ingestions} == {"asset-a", "asset-b"}
