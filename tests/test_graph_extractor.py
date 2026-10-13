import json
from types import SimpleNamespace

from core.graph_extractor import (
    GRAPH_EXTRACTION_RESPONSE_SCHEMA,
    GraphExtractor,
    _compact_community_payload,
    _graph_prompt_char_budget,
)


class _GraphStore:
    def __init__(self):
        self.entities = []
        self.relationships = []

    def add_entity(self, *args, **kwargs):
        self.entities.append((args, kwargs))

    def add_relationship(self, *args, **kwargs):
        self.relationships.append((args, kwargs))


class _StructuredModel:
    def __init__(self):
        self.kwargs = None

    def create_chat_completion(
        self,
        *,
        messages,
        temperature,
        max_tokens,
        repeat_penalty,
        response_format=None,
    ):
        self.kwargs = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "repeat_penalty": repeat_penalty,
            "response_format": response_format,
        }
        return {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "entities": [{
                            "name": "RAG",
                            "type": "技术",
                            "description": "检索增强生成",
                        }],
                        "relationships": [],
                    }, ensure_ascii=False),
                },
            }],
        }


class _ReportModel:
    def __init__(self):
        self.messages = None

    def create_chat_completion(self, *, messages, temperature, max_tokens, repeat_penalty):
        self.messages = messages
        return {"choices": [{"message": {"content": "社区摘要 [E1]"}}]}


def _runtime_config(*, graph_n_ctx=8192, graph_max_tokens=2048):
    return SimpleNamespace(
        graph_n_ctx=graph_n_ctx,
        graph_max_tokens=graph_max_tokens,
        graph_temperature=0.1,
        graph_repeat_penalty=1.1,
    )


def test_graph_extraction_uses_schema_when_model_declares_response_format():
    extractor = object.__new__(GraphExtractor)
    extractor.runtime_config = _runtime_config()
    extractor.llm = _StructuredModel()
    extractor.db = _GraphStore()

    result = extractor.extract_and_store("RAG 使用向量检索补充生成上下文。", chunk_ids=["chunk-1"])

    assert result["entities"][0]["name"] == "RAG"
    assert extractor.llm.kwargs["response_format"] == GRAPH_EXTRACTION_RESPONSE_SCHEMA
    assert extractor.db.entities[0][1]["chunk_ids"] == ["chunk-1"]


def test_community_report_input_is_bounded_and_remains_valid_json():
    extractor = object.__new__(GraphExtractor)
    extractor.runtime_config = _runtime_config(graph_n_ctx=4096, graph_max_tokens=800)
    extractor.llm = _ReportModel()

    community = {
        "community_id": "C001",
        "title": "RAG / GraphRAG",
        "entity_names": [f"实体-{index}-" + "很长" * 40 for index in range(40)],
        "source_filenames": [f"资料-{index}.pdf" for index in range(20)],
        "chunk_ids": [f"chunk-{index}" for index in range(100)],
        "relationships": ["实体A --(关系)--> 实体B" + "扩展" * 80 for _ in range(30)],
        "document_summaries": [
            {
                "source_filename": f"资料-{index}.pdf",
                "summary": "摘要" * 800,
                "keywords": [f"关键词-{item}" for item in range(20)],
            }
            for index in range(10)
        ],
    }

    assert extractor.generate_community_report(community) == "社区摘要 [E1]"
    user_payload = extractor.llm.messages[1]["content"]
    parsed = json.loads(user_payload)
    assert len(user_payload) <= _graph_prompt_char_budget(4096, 800)
    assert parsed["community_id"] == "C001"
    assert "chunk_ids" not in parsed
