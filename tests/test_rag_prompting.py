import unittest
from types import SimpleNamespace

from core.rag_engine import RAGEngine, build_answer_messages


class RagPromptingTests(unittest.TestCase):
    def test_build_answer_messages_emits_clean_instruction_text(self):
        messages = build_answer_messages(
            query="这个方案怎么保护隐私？",
            vector_context="E1: 该方案使用本地加密存储。",
            graph_context="",
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("只能依据", messages[0]["content"])
        self.assertIn("<context>", messages[1]["content"])
        self.assertIn("本地加密存储", messages[1]["content"])

    def test_build_answer_messages_requires_synthesis_not_context_copying(self):
        messages = build_answer_messages(
            query="传统企业知识管理系统主要存在哪些问题？",
            vector_context=(
                "[E1 | 来源: 大模型知识管理系统.pdf | 章节: 摘要 | 切片: 0]\n"
                "传统企业知识管理系统存在构建成本高、知识利用率低的问题。"
            ),
            graph_context="",
        )

        prompt_text = "\n".join(message["content"] for message in messages)

        self.assertIn("归纳", prompt_text)
        self.assertIn("不要复制", prompt_text)
        self.assertIn("回答用户问题", prompt_text)
        self.assertIn("传统企业知识管理系统主要存在哪些问题？", prompt_text)

    def test_generate_response_stream_retries_once_for_unsupported_citation(self):
        engine = _build_engine(
            llm=_FakeStreamingLlm(
                [
                    "回答引用错误 [E9]",
                    "回答引用正确 [E1]",
                ]
            ),
            evidence=[
                {
                    "id": "E1",
                    "score": 0.2,
                    "source_filename": "privacy.pdf",
                    "snippet": "这个方案通过本地加密存储保护隐私。",
                }
            ],
        )

        output = "".join(engine.generate_response_stream("这个方案怎么保护隐私？"))

        self.assertIn("回答引用正确 [E1]", output)
        self.assertEqual(engine.llm.call_count, 2)

    def test_generate_response_stream_repairs_missing_citation_without_retrying(self):
        engine = _build_engine(
            llm=_FakeStreamingLlm(["该方案通过本地加密存储保护隐私。"]),
            evidence=[
                {
                    "id": "E1",
                    "score": 0.2,
                    "source_filename": "privacy.pdf",
                    "snippet": "该方案通过本地加密存储保护隐私。",
                }
            ],
        )

        output = "".join(engine.generate_response_stream("这个方案怎么保护隐私？"))

        self.assertIn("该方案通过本地加密存储保护隐私。 [E1]", output)
        self.assertEqual(engine.llm.call_count, 1)

    def test_generate_response_stream_does_not_repair_unrelated_missing_citation(self):
        engine = _build_engine(
            llm=_FakeStreamingLlm(["今天天气很好。", "该方案通过本地加密存储保护隐私。 [E1]"]),
            evidence=[
                {
                    "id": "E1",
                    "score": 0.2,
                    "source_filename": "privacy.pdf",
                    "snippet": "该方案通过本地加密存储保护隐私。",
                }
            ],
        )

        output = "".join(engine.generate_response_stream("这个方案怎么保护隐私？"))

        self.assertIn("该方案通过本地加密存储保护隐私。 [E1]", output)
        self.assertNotIn("今天天气很好。 [E1]", output)
        self.assertEqual(engine.llm.call_count, 2)

    def test_generate_response_stream_uses_synthesized_fallback_after_second_invalid_answer(self):
        engine = _build_engine(
            llm=_FakeStreamingLlm(
                [
                    "第一次还是错的 [E9]",
                    "第二次还是错的 [E9]",
                ]
            ),
            evidence=[
                {
                    "id": "E1",
                    "score": 0.2,
                    "source_filename": "大模型知识管理系统.pdf",
                    "section": "摘要",
                    "snippet": (
                        "大模型知识管理系统 Large Model Knowledge Management System 周扬 / ZHOU Yang，"
                        "摘要：企业知识管理至关重要，而传统企业知识管理系统存在构建成本高、"
                        "知识利用率低的问题。提出了基于大模型检索增强生成（RAG）技术构建企业知识管理系统的方案。"
                    ),
                }
            ],
        )

        output = "".join(engine.generate_response_stream("传统企业知识管理系统主要存在哪些问题？"))
        answer_text = output.split("```souldrive-evidence", 1)[0]

        self.assertIn("传统企业知识管理系统的主要问题包括", answer_text)
        self.assertIn("构建成本高", answer_text)
        self.assertIn("知识利用率低", answer_text)
        self.assertIn("[E1]", answer_text)
        self.assertNotIn("Large Model Knowledge Management System", answer_text)
        self.assertNotIn("context trimmed", answer_text)
        self.assertNotIn("保守结论", answer_text)
        self.assertEqual(engine.llm.call_count, 2)

    def test_generate_response_stream_does_not_generate_for_retry_gate_evidence(self):
        engine = _build_engine(
            llm=_FakeStreamingLlm(["不应该生成 [E1]"]),
            evidence=[{"id": "E1", "score": 0.055, "source_filename": "weak.pdf"}],
        )

        output = "".join(engine.generate_response_stream("这个资料能证明什么？"))

        self.assertIn("未找到足够可靠的相关证据", output)
        self.assertEqual(engine.llm.call_count, 0)

    def test_generate_response_stream_falls_back_to_evidence_when_citations_keep_failing(self):
        engine = _build_engine(
            llm=_FakeStreamingLlm(["wrong citation [E9]", "still wrong [E9]"]),
            evidence=[
                {
                    "id": "E1",
                    "score": 0.2,
                    "source_filename": "privacy.pdf",
                    "snippet": "The system uses local encrypted storage.",
                }
            ],
        )

        output = "".join(engine.generate_response_stream("privacy?"))

        self.assertIn("主要信息包括", output)
        self.assertIn("local encrypted storage", output)
        self.assertIn("[E1]", output)
        self.assertNotIn("evidence accepted", output)
        self.assertEqual(engine.llm.call_count, 2)

    def test_transformer_query_uses_generation_instead_of_hardcoded_fast_answer(self):
        engine = _build_engine(
            llm=_FakeStreamingLlm(["模型生成的 Transformer 回答 [E1]"]),
            evidence=[
                {
                    "id": "E1",
                    "score": 0.2,
                    "source_filename": "attention.pdf",
                    "snippet": "Transformer 是一种基于 Attention 的模型架构。",
                }
            ],
        )

        output = "".join(engine.generate_response_stream("Transformer 是什么？"))

        self.assertIn("模型生成的 Transformer 回答 [E1]", output)
        self.assertEqual(engine.llm.call_count, 1)

    def test_generate_response_stream_defaults_to_six_evidence_items(self):
        engine = _build_engine(
            llm=_FakeStreamingLlm(["answer [E1]"]),
            evidence=[
                {
                    "id": "E1",
                    "score": 0.2,
                    "source_filename": "privacy.pdf",
                    "snippet": "The system uses local encrypted storage.",
                }
            ],
        )

        "".join(engine.generate_response_stream("privacy?"))

        self.assertEqual(engine.kb.last_top_k, 6)


def _build_engine(llm, evidence):
    engine = RAGEngine.__new__(RAGEngine)
    engine.kb = _FakeKnowledgeBase(evidence)
    engine.graph_db = None
    engine.llm = llm
    engine.audit_logger = _FakeAuditLogger()
    engine.runtime_config = SimpleNamespace(
        temperature=0.01,
        top_p=0.85,
        repeat_penalty=1.12,
        max_tokens=256,
    )
    return engine


class _FakeKnowledgeBase:
    def __init__(self, evidence):
        self.evidence = evidence
        self.last_top_k = None

    def search_with_evidence(self, query, graph_db=None, top_k=3):
        _ = query
        _ = graph_db
        self.last_top_k = top_k
        return {
            "documents": ["该方案使用本地加密存储。"],
            "metadatas": [{"source_filename": "privacy.pdf", "page": 1, "chunk_index": 0}],
            "graph_context": [],
            "evidence": self.evidence,
            "retrieval_mode": "test",
            "matched_entities": [],
        }


class _FakeStreamingLlm:
    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    def create_chat_completion(self, messages, stream, temperature, top_p, repeat_penalty, max_tokens, stop):
        _ = messages
        _ = stream
        _ = temperature
        _ = top_p
        _ = repeat_penalty
        _ = max_tokens
        _ = stop
        content = self.responses[self.call_count]
        self.call_count += 1
        return iter(
            [
                {"choices": [{"delta": {"content": content}}]},
            ]
        )


class _FakeAuditLogger:
    def append_event(self, event_type, payload, trace_id=None):
        _ = event_type
        _ = payload
        _ = trace_id


if __name__ == "__main__":
    unittest.main()
