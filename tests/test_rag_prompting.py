import unittest
from types import SimpleNamespace

from core.rag_engine import RAGEngine, build_answer_messages, build_vector_context


class RagPromptingTests(unittest.TestCase):
    def test_vector_context_uses_evidence_source_for_legacy_metadata(self):
        context = build_vector_context(
            ["企业技术方案正文"],
            [{"document_title": "企业技术方案"}],
            [{"id": "E1", "source_filename": "企业技术方案"}],
        )

        self.assertIn("来源: 企业技术方案", context)
        self.assertNotIn("未知文件", context)

    def test_build_answer_messages_emits_clean_instruction_text(self):
        messages = build_answer_messages(
            query="这个方案怎么保护隐私？",
            vector_context="E1: 该方案使用本地加密存储。",
            graph_context="",
        )

        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("只能依据", messages[0]["content"])
        self.assertIn("提示注入", messages[0]["content"])
        self.assertIn("Agent 状态栏", messages[0]["content"])
        self.assertIn("<context>", messages[1]["content"])
        self.assertIn("【Agent 状态栏】", messages[1]["content"])
        self.assertIn("本地加密存储", messages[1]["content"])
        self.assertEqual(messages[1]["content"].count("【私有文献详情】"), 1)

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

    def test_build_answer_messages_includes_learning_context_outside_document_evidence(self):
        messages = build_answer_messages(
            query="以后什么时候保存记忆？",
            vector_context="未检索到直接相关的文献片段。",
            graph_context="",
            learning_context={
                "memories": [
                    {
                        "title": "关闭前集中审核记忆",
                        "content": "用户希望关闭前集中审核候选记忆，不希望每轮问答后都弹窗。",
                        "score": 0.9,
                    }
                ],
                "skills": [
                    {
                        "title": "实现前测试",
                        "content": "先写失败测试，再实现最小代码。",
                        "score": 0.8,
                    }
                ],
            },
        )

        system_prompt = messages[0]["content"]
        prompt = messages[-1]["content"]
        self.assertIn("长期记忆", system_prompt)
        self.assertIn("不要把长期记忆或技能当作文档证据引用", system_prompt)
        self.assertIn("【已确认长期记忆】", prompt)
        self.assertIn("关闭前集中审核记忆", prompt)
        self.assertIn("【已确认可复用技能】", prompt)
        self.assertIn("实现前测试", prompt)
        self.assertLess(prompt.index("【Agent 状态栏】"), prompt.index("【已确认长期记忆】"))
        self.assertLess(prompt.index("【已确认长期记忆】"), prompt.index("【私有文献详情】"))
        document_section = prompt.split("【私有文献详情】", 1)[1]
        self.assertNotIn("关闭前集中审核记忆", document_section)

    def test_build_answer_messages_accepts_runtime_status_bar(self):
        messages = build_answer_messages(
            query="这个方案怎么保护隐私？",
            vector_context="[E1] 本地加密存储。",
            graph_context="",
            status_bar="【Agent 状态栏】\n- evidence_count: 1\n- evidence_gate: accept",
        )

        prompt = messages[-1]["content"]
        self.assertIn("- evidence_count: 1", prompt)
        self.assertLess(prompt.index("【Agent 状态栏】"), prompt.index("【私有文献详情】"))

    def test_build_answer_messages_includes_memory_capsule_and_skill_execution(self):
        messages = build_answer_messages(
            query="Please implement this feature with tests first",
            vector_context="No direct document evidence.",
            graph_context="",
            learning_context={
                "memory_capsule": {
                    "text": "- Review before saving: User wants human review before long-term memory writes.",
                    "items": [{"memory_id": "mem_pref"}],
                },
                "memories": [],
                "skills": [],
                "skill_execution": {
                    "skill_title": "TDD workflow",
                    "steps": [
                        {"index": 1, "instruction": "Write a failing test"},
                        {"index": 2, "instruction": "Implement minimal code"},
                    ],
                    "output_contract": "Return implementation summary and verification.",
                    "success_criteria": ["Tests pass"],
                },
            },
        )

        prompt = messages[-1]["content"]
        self.assertIn("Review before saving", prompt)
        self.assertIn("TDD workflow", prompt)
        self.assertIn("Write a failing test", prompt)
        self.assertIn("Return implementation summary and verification", prompt)
        self.assertLess(prompt.index("Review before saving"), prompt.index("No direct document evidence."))

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

    def test_generate_response_stream_retries_retrieval_before_refusing_weak_evidence(self):
        engine = _build_engine(
            llm=_FakeStreamingLlm(["不应该生成 [E1]"]),
            evidence=[{"id": "E1", "score": 0.055, "source_filename": "weak.pdf"}],
        )

        output = "".join(engine.generate_response_stream("这个资料能证明什么？"))

        self.assertIn("未找到足够可靠的相关证据", output)
        self.assertEqual(engine.llm.call_count, 0)
        self.assertEqual(engine.kb.top_k_calls, [6, 8])

    def test_generate_response_stream_uses_stronger_retry_evidence(self):
        engine = _build_engine(
            llm=_FakeStreamingLlm(["扩大检索后找到证据 [E1]"]),
            evidence=[{"id": "E1", "score": 0.055, "source_filename": "weak.pdf"}],
            retry_evidence=[
                {
                    "id": "E1",
                    "score": 0.2,
                    "source_filename": "privacy.pdf",
                    "snippet": "该方案使用本地加密存储保护隐私。",
                }
            ],
        )

        output = "".join(engine.generate_response_stream("这个方案怎么保护隐私？"))

        self.assertIn("扩大检索后找到证据 [E1]", output)
        self.assertEqual(engine.llm.call_count, 1)
        self.assertEqual(engine.kb.top_k_calls, [6, 8])

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


def _build_engine(llm, evidence, retry_evidence=None):
    engine = RAGEngine.__new__(RAGEngine)
    engine.kb = _FakeKnowledgeBase(evidence, retry_evidence=retry_evidence)
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
    def __init__(self, evidence, retry_evidence=None):
        self.evidence_runs = [evidence, retry_evidence if retry_evidence is not None else evidence]
        self.last_top_k = None
        self.top_k_calls = []

    def search_with_evidence(self, query, graph_db=None, top_k=3):
        _ = query
        _ = graph_db
        self.last_top_k = top_k
        self.top_k_calls.append(top_k)
        run_index = min(len(self.top_k_calls) - 1, len(self.evidence_runs) - 1)
        return {
            "documents": ["该方案使用本地加密存储。"],
            "metadatas": [{"source_filename": "privacy.pdf", "page": 1, "chunk_index": 0}],
            "graph_context": [],
            "evidence": self.evidence_runs[run_index],
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
