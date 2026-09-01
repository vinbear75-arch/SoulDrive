"""Read-only acceptance probes for SoulDrive RAG routing.

The script creates only temporary workspaces and writes no project production
files. It exercises the HTTP project/session/chat path and the RAG engine path
with a real LocalKnowledgeBase populated by synthetic evidence.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(__file__.replace("\\", "/").rsplit("/", 2)[0]))

from fastapi.testclient import TestClient

from core import mcp_server
from core.knowledge_base import LocalKnowledgeBase
from core.rag_engine import RAGEngine
from core.security_context import clear_workspace_keys, set_workspace_keys
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class FakeChatRag:
    def __init__(self) -> None:
        self.interaction_context = None

    def generate_response_stream(self, query, top_k=3, trace_id=None, learning_context=None, interaction_context=None):
        self.interaction_context = interaction_context
        yield "已进入普通 RAG 回答"


class FakeChunk:
    def __init__(self, content: str, metadata: dict) -> None:
        self.page_content = content
        self.metadata = metadata


class FakeEmbeddingResult(list):
    def tolist(self):
        return list(self)


class FakeEmbeddingModel:
    def encode(self, texts, normalize_embeddings=True):
        if isinstance(texts, str):
            texts = [texts]
        vectors = []
        for text in texts:
            lowered = str(text).lower()
            vectors.append([
                float("rag" in lowered or "检索" in lowered or "生成" in lowered),
                float("cafeteria" in lowered or "parking" in lowered),
            ])
        return FakeEmbeddingResult(vectors)


class CapturingLlm:
    def __init__(self) -> None:
        self.messages = []

    def create_chat_completion(self, messages, stream, temperature, top_p, repeat_penalty, max_tokens, stop):
        self.messages = messages
        yield {"choices": [{"delta": {"content": "RAG 先检索全局文档证据。[E1] 截图显示本地模式。[V1]"}}]}


class NoopAudit:
    def append_event(self, *_args, **_kwargs):
        return None


def mounted_state(workspace):
    return {
        "locked": False,
        "software_unlocked": True,
        "workspace_path": workspace.root_path,
        "auth_level": "HARDWARE_PLUS_PASSWORD",
        "indexing": {"status": "idle"},
    }


def verify_new_project_session_chat() -> dict:
    original_rag = mcp_server.rag
    original_kb = mcp_server.kb
    original_loaded = mcp_server.loaded_workspace_path
    with tempfile.TemporaryDirectory(prefix="souldrive-acceptance-") as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        initialize_keystore(workspace, "acceptance-passphrase")
        keys = unlock_keystore(workspace, "acceptance-passphrase")
        set_workspace_keys(workspace.root_path, keys)
        state = mounted_state(workspace)
        fake_rag = FakeChatRag()
        client = TestClient(mcp_server.app)
        headers = {"X-SoulDrive-Token": "acceptance-token"}
        project_id = f"acceptance-project-{uuid.uuid4().hex[:8]}"
        session_id = f"acceptance-session-{uuid.uuid4().hex[:8]}"
        try:
            with patch.dict(os.environ, {"SOULDRIVE_API_TOKEN": "acceptance-token"}, clear=False), \
                patch.object(mcp_server, "get_runtime_state", return_value=state), \
                patch.object(mcp_server, "effective_runtime_state", return_value=state), \
                patch.object(mcp_server, "current_workspace", return_value=workspace), \
                patch.object(mcp_server, "ensure_runtime_loaded", side_effect=lambda: setattr(mcp_server, "rag", fake_rag)):
                project = client.post("/projects", json={"project_id": project_id, "name": "验收项目"}, headers=headers)
                session = client.post(
                    f"/projects/{project_id}/sessions",
                    json={"session_id": session_id, "title": "新会话"},
                    headers=headers,
                )
                chat = client.post(
                    "/chat",
                    json={
                        "query": "RAG 的核心思想是什么？",
                        "session_id": session_id,
                        "active_context": {
                            "project_id": project_id,
                            "document_names": [],
                        },
                    },
                    headers=headers,
                )
            captured = fake_rag.interaction_context or {}
            active = captured.get("active_context") or {}
            return {
                "project_status": project.status_code,
                "session_status": session.status_code,
                "chat_status": chat.status_code,
                "chat_body": chat.text,
                "captured_project_id": active.get("project_id"),
                "captured_document_names": active.get("document_names"),
                "ordinary_rag_reached": chat.status_code == 200 and "请明确要处理的文档" not in chat.text,
            }
        finally:
            mcp_server._close_session_store()
            clear_workspace_keys()
            mcp_server.rag = original_rag
            mcp_server.kb = original_kb
            mcp_server.loaded_workspace_path = original_loaded


def verify_visual_plus_global_document_evidence() -> dict:
    with tempfile.TemporaryDirectory(prefix="souldrive-acceptance-") as temp_dir:
        workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
        initialize_keystore(workspace, "acceptance-passphrase")
        keys = unlock_keystore(workspace, "acceptance-passphrase")
        set_workspace_keys(workspace.root_path, keys)
        workspace_path = workspace.models_path
        os.makedirs(os.path.join(workspace_path, "bge-small-zh-v1.5"), exist_ok=True)
        with patch("core.knowledge_base._sentence_transformer", return_value=FakeEmbeddingModel()):
            kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
            try:
                kb.ingest_chunks(
                    "global-rag-doc",
                    [FakeChunk("RAG 先从全局知识库检索相关证据，再生成回答。", {"source_filename": "global-rag.pdf", "page": 1})],
                )
                kb.ingest_chunks(
                    "project-noise-doc",
                    [FakeChunk("cafeteria menu and parking instructions", {"source_filename": "project-noise.pdf", "page": 1, "project_id": "acceptance-project"})],
                )
                llm = CapturingLlm()
                engine = RAGEngine.__new__(RAGEngine)
                engine.kb = kb
                engine.graph_db = None
                engine.llm = llm
                engine.audit_logger = NoopAudit()
                engine.runtime_config = SimpleNamespace(temperature=0.01, top_p=0.85, repeat_penalty=1.12, max_tokens=256)
                output = "".join(engine.generate_response_stream(
                    "RAG 的核心思想是什么？",
                    top_k=3,
                    interaction_context={
                        "session_id": "acceptance-session",
                        "active_context": {"project_id": "acceptance-project"},
                        "events": [{
                            "event_id": "image-acceptance",
                            "modality": "image",
                            "timestamp_ms": 1000,
                            "text": "截图显示本地模式。",
                            "confidence": 0.95,
                            "bbox": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
                        }],
                    },
                ))
                prompt_text = json.dumps(llm.messages, ensure_ascii=False)
                retrieval = kb.search_with_evidence("RAG 的核心思想是什么？", top_k=3, project_id="acceptance-project")
                sources = [item.get("source_filename") for item in retrieval.get("metadatas") or []]
                result = {
                    "output": output,
                    "model_prompt_has_global_source": "global-rag.pdf" in prompt_text,
                    "model_prompt_has_visual_text": "截图显示本地模式" in prompt_text,
                    "answer_has_document_citation": "[E1]" in output,
                    "answer_has_visual_citation": "[V1]" in output,
                    "retrieval_sources": sources,
                    "project_noise_excluded": "project-noise.pdf" not in sources,
                    "route": (retrieval.get("observability") or {}).get("project_focus", {}).get("route"),
                }
                assert result["model_prompt_has_global_source"]
                assert result["model_prompt_has_visual_text"]
                assert result["answer_has_document_citation"]
                assert result["answer_has_visual_citation"]
                assert result["project_noise_excluded"]
                assert result["route"] == "global_fallback"
                return result
            finally:
                kb.close()
                clear_workspace_keys()


if __name__ == "__main__":
    ordinary = verify_new_project_session_chat()
    assert ordinary["project_status"] == 200
    assert ordinary["session_status"] == 200
    assert ordinary["chat_status"] == 200
    assert ordinary["ordinary_rag_reached"]
    print(json.dumps({
        "new_project_session_chat": ordinary,
        "visual_plus_global_document": verify_visual_plus_global_document_evidence(),
    }, ensure_ascii=False, indent=2))
