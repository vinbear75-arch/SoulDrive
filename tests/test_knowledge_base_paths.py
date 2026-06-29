import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.knowledge_base import LocalKnowledgeBase
from core.knowledge_base import _persistent_client
from core.workspace import SoulDriveWorkspace


class KnowledgeBasePathTests(unittest.TestCase):
    def test_knowledge_base_requires_explicit_workspace_or_index_path(self):
        with self.assertRaisesRegex(ValueError, "workspace_path"):
            LocalKnowledgeBase()

    def test_workspace_path_uses_workspace_index_paths_without_local_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True)
            chroma_client = MagicMock()

            with (
                patch("core.knowledge_base._persistent_client", return_value=chroma_client) as persistent_client,
                patch("core.knowledge_base._sentence_transformer"),
            ):
                kb = LocalKnowledgeBase(workspace_path=workspace.root_path)
                kb.close()

        persistent_client.assert_called_once_with(path=workspace.chroma_path)

    def test_embedding_model_can_be_loaded_from_workspace_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True)
            chroma_client = MagicMock()

            with (
                patch("core.knowledge_base._persistent_client", return_value=chroma_client),
                patch("core.knowledge_base.resolve_torch_device", return_value="cuda"),
                patch("core.knowledge_base._sentence_transformer") as sentence_transformer,
            ):
                kb = LocalKnowledgeBase(
                    db_path=workspace.chroma_path,
                    parent_doc_path=workspace.parent_doc_path,
                    keyword_index_path=workspace.keyword_index_path,
                    workspace_path=workspace.root_path,
                )
                kb.close()

        sentence_transformer.assert_called_once_with(str(embedding_model_path))

    def test_embedding_model_uses_resolved_torch_device(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = SoulDriveWorkspace.from_drive(temp_dir).ensure()
            embedding_model_path = Path(workspace.models_path) / "bge-small-zh-v1.5"
            embedding_model_path.mkdir(parents=True)
            chroma_client = MagicMock()

            with (
                patch("core.knowledge_base._persistent_client", return_value=chroma_client),
                patch("core.knowledge_base.resolve_torch_device", return_value="cuda:0"),
                patch("sentence_transformers.SentenceTransformer") as sentence_transformer,
            ):
                kb = LocalKnowledgeBase(
                    db_path=workspace.chroma_path,
                    parent_doc_path=workspace.parent_doc_path,
                    keyword_index_path=workspace.keyword_index_path,
                    workspace_path=workspace.root_path,
                )
                kb.close()

        sentence_transformer.assert_called_once_with(
            str(embedding_model_path), local_files_only=True, device="cuda:0"
        )

    def test_persistent_client_disables_chroma_anonymized_telemetry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("chromadb.PersistentClient") as persistent_client:
                _persistent_client(temp_dir)

        settings = persistent_client.call_args.kwargs["settings"]
        self.assertFalse(settings.anonymized_telemetry)


if __name__ == "__main__":
    unittest.main()
