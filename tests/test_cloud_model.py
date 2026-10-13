import json
import io
import os
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from fastapi.testclient import TestClient

from core import cloud_model, mcp_server
from core.cloud_model import CloudModelConfig, OpenAICompatibleChatModel
from core.rag_engine import RAGEngine


class _FakeResponse:
    def __init__(self, body: bytes, lines: list[bytes] | None = None):
        self.body = body
        self.lines = lines or []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self.body

    def __iter__(self):
        return iter(self.lines)


class CloudModelTests(unittest.TestCase):
    def test_normalize_base_url_accepts_base_or_chat_endpoint(self):
        self.assertEqual(
            cloud_model.normalize_base_url("https://api.example.com/v1/chat/completions"),
            "https://api.example.com/v1",
        )
        self.assertEqual(
            cloud_model.normalize_base_url("https://api.example.com/v1/models"),
            "https://api.example.com/v1",
        )

    def test_stream_adapter_sends_bearer_key_and_parses_sse(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _FakeResponse(b"", [
                'data: {"choices":[{"delta":{"content":"你好"}}]}\n'.encode("utf-8"),
                b"\n",
                'data: {"choices":[{"delta":{"content":"，云端"}}]}\n'.encode("utf-8"),
                b"data: [DONE]\n",
            ])

        with patch.object(cloud_model, "urlopen", side_effect=fake_urlopen):
            chunks = list(OpenAICompatibleChatModel(
                "https://api.example.com/v1",
                "test-key",
                "demo-model",
            ).create_chat_completion(messages=[], stream=True))

        self.assertEqual("".join(chunk["choices"][0]["delta"]["content"] for chunk in chunks), "你好，云端")
        self.assertEqual(captured["request"].full_url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(captured["request"].headers["Authorization"], "Bearer test-key")
        self.assertEqual(captured["timeout"], 180)

    def test_model_list_accepts_openai_data_shape(self):
        response = _FakeResponse(json.dumps({"data": [{"id": "model-a"}, {"id": "model-a"}, {"id": "model-b"}]}).encode())
        with patch.object(cloud_model, "urlopen", return_value=response):
            models = cloud_model.fetch_cloud_models("https://api.example.com/v1", "test-key")
        self.assertEqual(models, ["model-a", "model-b"])

    def test_root_base_url_prefers_v1_for_model_list(self):
        captured = {}
        response = _FakeResponse(json.dumps({"data": [{"id": "model-a"}]}).encode())

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return response

        with patch.object(cloud_model, "urlopen", side_effect=fake_urlopen):
            models = cloud_model.fetch_cloud_models("https://api.example.com", "test-key")

        self.assertEqual(models, ["model-a"])
        self.assertEqual(captured["url"], "https://api.example.com/v1/models")

    def test_root_base_url_falls_back_when_v1_is_not_supported(self):
        captured = []
        response = _FakeResponse(json.dumps({"data": [{"id": "model-a"}]}).encode())

        def fake_urlopen(request, timeout):
            captured.append(request.full_url)
            if len(captured) == 1:
                raise HTTPError(request.full_url, 404, "not found", {}, io.BytesIO(b""))
            return response

        with patch.object(cloud_model, "urlopen", side_effect=fake_urlopen):
            models = cloud_model.fetch_cloud_models("https://api.example.com", "test-key")

        self.assertEqual(models, ["model-a"])
        self.assertEqual(captured, [
            "https://api.example.com/v1/models",
            "https://api.example.com/models",
        ])

    def test_root_base_url_prefers_v1_for_chat_completion(self):
        captured = {}
        response = _FakeResponse(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return response

        with patch.object(cloud_model, "urlopen", side_effect=fake_urlopen):
            result = OpenAICompatibleChatModel(
                "https://api.example.com",
                "test-key",
                "model-a",
            ).create_chat_completion(messages=[])

        self.assertEqual(result["choices"][0]["message"]["content"], "ok")
        self.assertEqual(captured["url"], "https://api.example.com/v1/chat/completions")

    def test_save_config_keeps_key_out_of_public_payload(self):
        stored = {}

        def read():
            return dict(stored) if stored else None

        def write(payload):
            stored.update(payload)

        with patch.object(cloud_model, "_read_credential", side_effect=read), patch.object(
            cloud_model, "_write_credential", side_effect=write
        ):
            saved = cloud_model.save_cloud_model_config(
                "https://api.example.com/v1",
                "test-key",
                "model-a",
                True,
                ["model-a", "model-b"],
            )

        self.assertTrue(saved.enabled)
        self.assertNotIn("api_key", saved.public_dict())
        self.assertTrue(saved.public_dict()["api_key_configured"])
        self.assertEqual(stored["api_key"], "test-key")

    def test_enabled_cloud_config_is_selected_before_local_model_loading(self):
        config = CloudModelConfig("https://api.example.com/v1", "test-key", "model-a", True, ("model-a",))
        engine = RAGEngine.__new__(RAGEngine)
        engine.llm = None
        engine.runtime_config = Mock(max_tokens=900)
        with patch("core.rag_engine.load_cloud_model_config", return_value=config), patch(
            "core.rag_engine.OpenAICompatibleChatModel"
        ) as cloud_model:
            engine._load_generation_model()

        cloud_model.assert_called_once_with("https://api.example.com/v1", "test-key", "model-a")
        self.assertIs(engine.llm, cloud_model.return_value)

    def test_config_status_endpoint_does_not_return_api_key(self):
        config = CloudModelConfig("https://api.example.com/v1", "test-key", "model-a", True, ("model-a",))
        with patch.dict(os.environ, {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False), patch.object(
            mcp_server, "effective_runtime_state", return_value={"locked": False, "software_unlocked": True}
        ), patch.object(mcp_server, "load_cloud_model_config", return_value=config):
            response = TestClient(mcp_server.app).get(
                "/cloud-model/config",
                headers={"X-SoulDrive-Token": "test-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "model-a")
        self.assertNotIn("api_key", response.json())

    def test_config_endpoint_saves_and_releases_current_generation_model(self):
        config = CloudModelConfig("https://api.example.com/v1", "test-key", "model-a", True, ("model-a",))
        saver = Mock(return_value=config)
        audit_logger = Mock()
        with patch.dict(os.environ, {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False), patch.object(
            mcp_server, "effective_runtime_state", return_value={"locked": False, "software_unlocked": True}
        ), patch.object(mcp_server, "save_cloud_model_config", saver), patch.object(
            mcp_server, "current_audit_logger", return_value=audit_logger
        ), patch.object(mcp_server, "rag", None):
            response = TestClient(mcp_server.app).post(
                "/cloud-model/config",
                headers={"X-SoulDrive-Token": "test-token"},
                json={
                    "base_url": "https://api.example.com/v1",
                    "api_key": "test-key",
                    "model": "model-a",
                    "enabled": True,
                    "models": ["model-a"],
                },
            )

        self.assertEqual(response.status_code, 200)
        saver.assert_called_once_with(
            "https://api.example.com/v1",
            "test-key",
            "model-a",
            True,
            ["model-a"],
        )
        self.assertNotIn("test-key", response.text)

    def test_config_endpoint_keeps_saved_config_when_audit_fails(self):
        config = CloudModelConfig("https://api.example.com/v1", "test-key", "model-a", True, ("model-a",))
        saver = Mock(return_value=config)

        class FailingAuditLogger:
            def append_event(self, *_args, **_kwargs):
                raise OSError("audit sink failed for secret-api-key")

        with patch.dict(os.environ, {"SOULDRIVE_API_TOKEN": "test-token"}, clear=False), patch.object(
            mcp_server, "effective_runtime_state", return_value={"locked": False, "software_unlocked": True}
        ), patch.object(mcp_server, "save_cloud_model_config", saver), patch.object(
            mcp_server, "current_audit_logger", return_value=FailingAuditLogger()
        ), patch.object(mcp_server, "rag", None):
            response = TestClient(mcp_server.app).post(
                "/cloud-model/config",
                headers={"X-SoulDrive-Token": "test-token"},
                json={
                    "base_url": "https://api.example.com/v1",
                    "api_key": "secret-api-key",
                    "model": "model-a",
                    "enabled": True,
                    "models": ["model-a"],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "model-a")
        self.assertIn("审计记录写入失败", response.json()["warning"])
        self.assertNotIn("secret-api-key", response.text)


if __name__ == "__main__":
    unittest.main()
