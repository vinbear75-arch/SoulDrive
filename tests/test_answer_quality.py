import json
import unittest
from decimal import MAX_EMAX, MIN_EMIN, Inexact, localcontext

from core.answer_quality import (
    citation_coverage,
    evaluate_audio_evidence_gate,
    evaluate_evidence_gate,
    evaluate_visual_evidence_gate,
    refusal_answer,
    validate_answer_citations,
)


class AnswerQualityTests(unittest.TestCase):
    def test_evidence_gate_rejects_non_finite_scores(self):
        gate = evaluate_evidence_gate([{"id": "E1", "score": float("nan")}])

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.decision, "reject")
        self.assertEqual(gate.max_score, 0.0)
        self.assertEqual(gate.reason, "evidence confidence below threshold")

    def test_evidence_gate_rejects_empty_or_low_confidence_evidence(self):
        empty = evaluate_evidence_gate([])
        low = evaluate_evidence_gate([{"id": "E1", "score": 0.001}])

        self.assertFalse(empty.allowed)
        self.assertFalse(low.allowed)
        self.assertEqual(empty.decision, "reject")
        self.assertEqual(low.decision, "reject")
        self.assertEqual(low.reason, "evidence confidence below threshold")

    def test_evidence_gate_requests_retry_for_mid_confidence_evidence(self):
        gate = evaluate_evidence_gate([{"id": "E1", "score": 0.055}])

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.decision, "retry")
        self.assertEqual(gate.reason, "evidence confidence requires retry")

    def test_evidence_gate_accepts_direct_single_source_definition_above_reject_floor(self):
        gate = evaluate_evidence_gate(
            [{
                "id": "E1",
                "score": 0.0203,
                "source_filename": "rag.md",
                "section": "RAG 原理",
                "snippet": "RAG 通过检索外部知识增强生成模型回答。",
            }],
            query="RAG 的核心思想是什么？",
        )

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "evidence accepted by direct answer")

    def test_evidence_gate_accepts_exact_quoted_section_hit(self):
        gate = evaluate_evidence_gate(
            [{
                "id": "E1",
                "score": 0.20,
                "section": "3. 教育",
                "snippet": "3. 教育",
            }],
            query="请定位资料中关于“3. 教育”的关键原文证据。",
        )

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "evidence accepted")

    def test_evidence_gate_uses_document_title_for_explicit_source_query(self):
        gate = evaluate_evidence_gate(
            [{
                "id": "E1",
                "score": 0.20,
                "document_title": "选区绑定界面示意",
                "source_filename": "08_selection_ui.png",
                "section": "图像文字区域 1",
                "snippet": "target_binding_accuracy 0.620 -> 0.790 -> 0.850",
            }],
            query=(
                "请定位“选区绑定界面示意”中的图像文字区域。"
            ),
        )

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "evidence accepted")

    def test_evidence_gate_accepts_visual_document_query_for_image_asset(self):
        gate = evaluate_evidence_gate(
            [{
                "id": "E1",
                "score": 0.20,
                "content_kind": "image",
                "document_title": "选区绑定界面示意",
                "source_filename": "08_selection_ui.png",
                "visual_asset_count": 1,
                "section": "图像文字区域 1",
                "snippet": "SoulDrive PC project workspace target_binding_accuracy 0.620 -> 0.790 -> 0.850",
            }],
            query=(
                "请从当前项目语料中查找“选区绑定界面示意”里关于“界面截图：当前项目为端侧多模态智能体优化项目，用户选中实验指标表中的 target_bindin”的记录。"
            ),
        )

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "evidence accepted")

    def test_evidence_gate_does_not_treat_document_title_as_requested_detail(self):
        gate = evaluate_evidence_gate(
            [{
                "id": "E1",
                "score": 0.20,
                "document_title": "视觉误检分析图",
                "source_filename": "vision_confusion_matrix.png",
                "section": "图像文字区域 1",
                "snippet": "target_binding_accuracy 0.620 -> 0.790 -> 0.850",
            }],
            query=(
                "请从当前项目语料中查找“视觉误检分析图”里关于“混淆矩阵截图：类别 C 的误检比例仍然偏高”的记录。"
            ),
        )

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "evidence does not answer query")

    def test_evidence_gate_keeps_single_source_comparison_in_retry(self):
        gate = evaluate_evidence_gate(
            [{
                "id": "E1",
                "score": 0.070,
                "source_filename": "甲方案.pdf",
                "snippet": "模型风险评估采用分级控制，安全审计按月核验。",
            }],
            query="比较两个方案在模型风险评估和安全审计上的做法。",
        )

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.decision, "retry")

    def test_evidence_gate_accepts_low_scored_corroborating_sources(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.070,
                    "source_filename": "甲方案.pdf",
                    "snippet": "模型风险评估采用分级控制，安全审计按月核验。",
                },
                {
                    "id": "E2",
                    "score": 0.066,
                    "source_filename": "乙方案.pdf",
                    "snippet": "模型风险评估采用动态授权，安全审计按季度验收。",
                },
            ],
            query="比较两个方案在模型风险评估和安全审计上的做法。",
        )

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.decision, "accept")
        self.assertEqual(gate.reason, "evidence accepted by corroboration")

    def test_evidence_gate_accepts_same_named_scoped_corroborating_sources(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.070,
                    "source_filename": "report.pdf",
                    "project_id": "project-a",
                    "document_id": "document-a",
                    "snippet": "模型风险评估采用分级控制，安全审计按月核验。",
                },
                {
                    "id": "E2",
                    "score": 0.066,
                    "source_filename": "report.pdf",
                    "project_id": "project-b",
                    "document_id": "document-b",
                    "snippet": "模型风险评估采用动态授权，安全审计按季度验收。",
                },
            ],
            query="比较两个项目在模型风险评估和安全审计上的做法。",
        )

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "evidence accepted by corroboration")

    def test_evidence_gate_does_not_treat_one_source_as_corroboration(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.070,
                    "source_filename": "甲方案.pdf",
                    "snippet": "模型风险评估采用分级控制，安全审计按月核验。",
                },
                {
                    "id": "E2",
                    "score": 0.066,
                    "source_filename": "甲方案.pdf",
                    "snippet": "模型风险评估采用分级控制，安全审计按季度检查。",
                },
            ],
            query="比较两个方案在模型风险评估和安全审计上的做法。",
        )

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.decision, "retry")

    def test_evidence_gate_requests_retry_when_query_is_not_answered_by_evidence(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.31,
                    "snippet": "传统企业知识管理系统存在构建成本高、知识利用率低的问题。",
                }
            ],
            query="SoulDrive 的创始人是谁？",
        )

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.decision, "retry")
        self.assertEqual(gate.reason, "evidence does not answer query")

    def test_evidence_gate_rejects_generic_overlap_for_weather_request(self):
        gate = evaluate_evidence_gate(
            [{
                "id": "E1",
                "score": 0.028,
                "source_filename": "rag-service.pdf",
                "snippet": "知识服务选择模型时需要考虑业务需求、数据安全、预算限制，并给出模型选择建议。",
            }],
            query="请根据当前本地资料给出上海明天的天气和穿衣建议。",
        )

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.decision, "retry")
        self.assertEqual(gate.reason, "evidence does not answer query")

    def test_evidence_gate_requires_named_identifier_for_license_price_request(self):
        gate = evaluate_evidence_gate(
            [{
                "id": "E1",
                "score": 0.039,
                "snippet": "BGE 是开源模型，采用 MIT 许可；商业嵌入模型按服务需求收费。",
            }],
            query="这些资料是否公布了 SoulDrive 的商业许可证价格？",
        )

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.decision, "retry")
        self.assertEqual(gate.reason, "evidence does not answer query")

    def test_evidence_gate_checks_bounded_top_six_when_relevant_source_is_ranked_later(self):
        evidence = [
            {
                "id": "E1",
                "score": 0.31,
                "source_filename": "项目预算.pdf",
                "snippet": "本项目预算和采购流程说明。",
            },
            {
                "id": "E2",
                "score": 0.28,
                "source_filename": "项目排期.pdf",
                "snippet": "本项目排期和人员安排说明。",
            },
            {
                "id": "E3",
                "score": 0.24,
                "source_filename": "项目采购.pdf",
                "snippet": "本项目采购审批说明。",
            },
            {
                "id": "E4",
                "score": 0.18,
                "source_filename": "rag.pdf",
                "snippet": "RAG 的核心思想是先从外部知识库检索相关证据，再生成回答。",
            },
        ]

        gate = evaluate_evidence_gate(evidence, query="RAG 的核心思想是什么？")

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "evidence accepted")

    def test_evidence_gate_does_not_accept_entity_only_overlap_for_attribute_query(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.31,
                    "snippet": "SoulDrive 是一个面向私有知识库的本地知识引擎。",
                }
            ],
            query="SoulDrive 的创始人是谁？",
        )

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "evidence does not answer query")

    def test_evidence_gate_accepts_paper_synthesis_with_matched_entity(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.31,
                    "source_filename": "self-rag_2023.pdf",
                    "snippet": "Self-RAG retrieves, generates and critiques passages.",
                }
            ],
            query="总结 Self-RAG 关于检索纠错的思路。",
        )

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.decision, "accept")

    def test_evidence_gate_splits_method_names_in_english_source_filenames(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.31,
                    "source_filename": "04_corrective_rag_2024.pdf",
                    "snippet": "The retriever may return low-quality documents.",
                },
                {
                    "id": "E2",
                    "score": 0.28,
                    "source_filename": "03_self_rag_2023.pdf",
                    "snippet": "The model critiques retrieved passages.",
                },
            ],
            query="Self-RAG 与 Corrective RAG 如何处理低质量检索？",
        )

        assert gate.allowed

    def test_evidence_gate_accepts_scored_evidence(self):
        gate = evaluate_evidence_gate(
            [
                {
                    "id": "E1",
                    "score": 0.2,
                    "snippet": "传统企业知识管理系统存在构建成本高、知识利用率低的问题。",
                },
                {"id": "E2", "score": 0.09},
            ],
            query="传统企业知识管理系统主要存在哪些问题？",
        )

        self.assertTrue(gate.allowed)
        self.assertEqual(gate.decision, "accept")
        self.assertEqual(gate.reason, "evidence accepted")

    def test_refusal_answer_is_explicit_about_local_evidence(self):
        gate = evaluate_evidence_gate([])
        answer = refusal_answer("What is GraphRAG?", gate)

        self.assertIn("未找到足够可靠的相关证据", answer)

    def test_citation_coverage_reports_used_and_unsupported_citations(self):
        report = citation_coverage("结论来自 [E1]，但 [E9] 不存在。", [{"id": "E1"}, {"id": "E2"}])

        self.assertEqual(report["coverage"], 0.5)
        self.assertEqual(report["used"], ["E1"])
        self.assertEqual(report["unsupported"], ["E9"])

    def test_validate_answer_citations_rejects_unsupported_citation(self):
        report = validate_answer_citations(
            "结论来自 [E1]，但 [E9] 也被引用。",
            [{"id": "E1"}, {"id": "E2"}],
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["decision"], "retry")
        self.assertEqual(report["unsupported"], ["E9"])

    def test_validate_answer_citations_rejects_missing_citation_when_evidence_exists(self):
        report = validate_answer_citations(
            "这里有结论，但是没有给出任何引用。",
            [{"id": "E1"}],
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["decision"], "retry")
        self.assertEqual(report["reason"], "answer missing evidence citation")

    def test_validate_answer_citations_requires_each_claim_to_be_cited(self):
        report = validate_answer_citations(
            "- 已被证据支持的结论。[E1]\n- 没有标注来源的第二条结论。",
            [{"id": "E1"}],
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["reason"], "answer claim missing evidence citation")
        self.assertEqual(report["uncited_claim_count"], 1)

    def test_validate_answer_citations_accepts_citations_for_each_claim(self):
        report = validate_answer_citations(
            "- 第一条结论。[E1]\n- 第二条结论。[E2]",
            [{"id": "E1"}, {"id": "E2"}],
        )

        self.assertTrue(report["valid"])

    def test_validate_answer_citations_allows_uncited_locator_scaffold(self):
        report = validate_answer_citations(
            "关于“2.2 系统流程”，资料中的直接证据表明：\n- RAG 由检索和生成组成。[E1]",
            [{"id": "E1"}],
        )

        self.assertTrue(report["valid"])

    def test_validate_answer_citations_semantic_support_rejects_unrelated_claim(self):
        report = validate_answer_citations(
            "RAG 会自动发送邮件，耗时 15 秒。[E1]",
            [{
                "id": "E1",
                "snippet": "RAG 通过检索外部知识增强模型回答。",
            }],
            semantic=True,
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["reason"], "citation does not support answer claim")
        self.assertEqual(report["semantic_support"]["unsupported_claim_count"], 1)

    def test_validate_answer_citations_rejects_missing_scoped_topic_in_cited_evidence(self):
        report = validate_answer_citations(
            (
                "根据证据中的第[19]切片，关于“3. graphrag的源码剖析”的关键原文证据是："
                "“图中的概念在构建索引中都已创建”。[E1]"
            ),
            [{
                "id": "E1",
                "section": "global查询",
                "snippet": "图中的概念在构建索引中都已创建。",
            }],
            semantic=True,
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["reason"], "citation does not support answer claim")
        self.assertEqual(
            report["semantic_support"]["claims"][0]["missing_scoped_topics"],
            ["3. graphrag的源码剖析"],
        )

    def test_validate_answer_citations_semantic_support_accepts_matching_number(self):
        report = validate_answer_citations(
            "本轮准确率为 85%。[V1]",
            [{
                "id": "V1",
                "modality": "image",
                "context": "柱状图显示本轮准确率为 85%。",
            }],
            semantic=True,
        )

        self.assertTrue(report["valid"])

    def test_validate_answer_citations_accepts_equivalent_decimal_and_percent(self):
        report = validate_answer_citations(
            "本轮准确率为 0.85。[V1]",
            [{
                "id": "V1",
                "modality": "image",
                "context": "图表显示本轮准确率为 85%。",
            }],
            semantic=True,
        )

        self.assertTrue(report["valid"])

    def test_validate_answer_citations_rejects_different_percent_value(self):
        report = validate_answer_citations(
            "本轮准确率为 0.86。[V1]",
            [{
                "id": "V1",
                "modality": "image",
                "context": "图表显示本轮准确率为 85%。",
            }],
            semantic=True,
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["semantic_support"]["unsupported_claim_count"], 1)

    def test_validate_answer_citations_rejects_changed_numeric_sign_or_exponent(self):
        for source_value, answer_value in (
            ("-5%", "+5%"),
            ("+5%", "-5%"),
            ("−5%", "5%"),
            ("1e-3", "1e+3"),
            ("-2.5 秒", "2500 ms"),
        ):
            with self.subTest(source_value=source_value, answer_value=answer_value):
                report = validate_answer_citations(
                    f"本轮实验测量值为 {answer_value}。[E1]",
                    [{"id": "E1", "snippet": f"本轮实验测量值为 {source_value}。"}],
                    semantic=True,
                )

                self.assertFalse(report["valid"])
                self.assertEqual(report["semantic_support"]["unsupported_claim_count"], 1)

    def test_validate_answer_citations_accepts_equivalent_signed_scientific_values(self):
        for source_value, answer_value in (
            ("-5%", "−0.05"),
            ("+5%", "5%"),
            ("1e-3", "0.001"),
            ("1e−3", "0.001"),
            ("1.0E+03 ms", "1 秒"),
            ("-2.5 秒", "−2500 ms"),
            ("范围 3-5", "范围 3 至 5"),
            ("model-8B", "model 8B"),
        ):
            with self.subTest(source_value=source_value, answer_value=answer_value):
                report = validate_answer_citations(
                    f"本轮实验测量值为 {answer_value}。[E1]",
                    [{"id": "E1", "snippet": f"本轮实验测量值为 {source_value}。"}],
                    semantic=True,
                )

                self.assertTrue(report["valid"], report)

    def test_validate_answer_citations_rejects_distinct_exact_numeric_values(self):
        for source_value, answer_value in (
            ("1000000000001", "1000000000002"),
            ("9007199254740992", "9007199254740993"),
            ("0.10000000000001", "0.10000000000002"),
            ("1.00000000000000000000000000001", "1.00000000000000000000000000002"),
            ("1e-400", "2e-400"),
            ("1e400", "2e400"),
            ("1e9999999", "2e9999999"),
            ("1e9999999999999999999", "2e9999999999999999999"),
            ("-.5%", "+.5%"),
            (".5", "5"),
            ("1.0000000000001 秒", "1000.0000000002 ms"),
            ("1.0000000000001 KiB", "1024.0000000002048 bytes"),
        ):
            with self.subTest(source_value=source_value, answer_value=answer_value):
                report = validate_answer_citations(
                    f"本轮实验测量值为 {answer_value}。[E1]",
                    [{"id": "E1", "snippet": f"本轮实验测量值为 {source_value}。"}],
                    semantic=True,
                )

                self.assertFalse(report["valid"], report)
                self.assertEqual(report["semantic_support"]["unsupported_claim_count"], 1)
                missing = report["semantic_support"]["claims"][0]["missing_fact_tokens"]
                self.assertTrue(missing)
                self.assertTrue(all(isinstance(token, str) and len(token) < 128 for token in missing))
                json.dumps(report, ensure_ascii=False, allow_nan=False)

    def test_validate_answer_citations_accepts_exact_numeric_equivalents(self):
        for source_value, answer_value in (
            ("-.5%", "-0.005"),
            ("+.5", "0.5"),
            ("1.000000000000000000000000000010", "1.00000000000000000000000000001"),
            ("1e-400", "10e-401"),
            ("1e400", "10e399"),
            ("1e9999999", "10e9999998"),
            ("1e-400%", "1e-402"),
            ("1e400 ms", "1e397 秒"),
            ("1.0000000000001 秒", "1000.0000000001 ms"),
            ("1.0000000000001 KiB", "1024.0000000001024 bytes"),
            ("-0%", "+0.000"),
        ):
            with self.subTest(source_value=source_value, answer_value=answer_value):
                report = validate_answer_citations(
                    f"本轮实验测量值为 {answer_value}。[E1]",
                    [{"id": "E1", "snippet": f"本轮实验测量值为 {source_value}。"}],
                    semantic=True,
                )

                self.assertTrue(report["valid"], report)
                json.dumps(report, ensure_ascii=False, allow_nan=False)

    def test_validate_answer_citations_preserves_callers_decimal_context(self):
        with localcontext() as context:
            context.prec = 2
            context.Emax = 2
            context.Emin = -2
            context.traps[Inexact] = True
            context.clear_flags()
            original_flags = dict(context.flags)
            for source_value, answer_value in (
                ("1.00000000000000000000000000001 秒", "1000.00000000000000000000000001 ms"),
                ("1e400 ms", "1e397 秒"),
                ("1e9999999999999999999", "1e9999999999999999999"),
            ):
                with self.subTest(source_value=source_value, answer_value=answer_value):
                    report = validate_answer_citations(
                        f"本轮实验测量值为 {answer_value}。[E1]",
                        [{"id": "E1", "snippet": f"本轮实验测量值为 {source_value}。"}],
                        semantic=True,
                    )
                    self.assertTrue(report["valid"], report)

            self.assertEqual(context.prec, 2)
            self.assertEqual(context.Emax, 2)
            self.assertEqual(context.Emin, -2)
            self.assertTrue(context.traps[Inexact])
            self.assertEqual(dict(context.flags), original_flags)

        with localcontext() as context:
            for signal in context.traps:
                context.traps[signal] = False
            context.clear_flags()
            original_flags = dict(context.flags)
            original_traps = dict(context.traps)
            for source_value, answer_value in (
                ("1e9999999999999999999", "2e9999999999999999999"),
                (f"1e{MAX_EMAX} 秒", f"2e{MAX_EMAX} 秒"),
                (f"1e{MIN_EMIN}%", f"2e{MIN_EMIN}%"),
            ):
                with self.subTest(source_value=source_value, answer_value=answer_value):
                    report = validate_answer_citations(
                        f"本轮实验测量值为 {answer_value}。[E1]",
                        [{"id": "E1", "snippet": f"本轮实验测量值为 {source_value}。"}],
                        semantic=True,
                    )
                    self.assertFalse(report["valid"], report)
                    json.dumps(report, ensure_ascii=False, allow_nan=False)

            self.assertEqual(dict(context.flags), original_flags)
            self.assertEqual(dict(context.traps), original_traps)

    def test_validate_answer_citations_accepts_equivalent_time_units(self):
        report = validate_answer_citations(
            "完整链路 P95 延迟为 2.5 秒。[E1]",
            [{
                "id": "E1",
                "snippet": "完整链路 P95 延迟为 2500 ms。",
            }],
            semantic=True,
        )

        self.assertTrue(report["valid"])

    def test_validate_answer_citations_rejects_different_time_units_value(self):
        report = validate_answer_citations(
            "完整链路 P95 延迟为 2.5 秒。[E1]",
            [{
                "id": "E1",
                "snippet": "完整链路 P95 延迟为 2600 ms。",
            }],
            semantic=True,
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["semantic_support"]["unsupported_claim_count"], 1)

    def test_validate_answer_citations_semantic_support_does_not_trust_title_only(self):
        report = validate_answer_citations(
            "文档明确要求系统自动发送邮件。[E1]",
            [{
                "id": "E1",
                "document_title": "系统要求说明",
                "source_filename": "requirements.pdf",
            }],
            semantic=True,
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["semantic_support"]["unsupported_claim_count"], 1)

    def test_validate_answer_citations_semantic_support_ignores_list_number(self):
        report = validate_answer_citations(
            "1. RAG 通过检索外部知识增强模型回答。[E1]",
            [{
                "id": "E1",
                "snippet": "RAG 通过检索外部知识增强模型回答。",
            }],
            semantic=True,
        )

        self.assertTrue(report["valid"])

    def test_validate_answer_citations_semantic_support_allows_source_locator_scaffold(self):
        report = validate_answer_citations(
            "来源文档：实验报告 [E1]\n\n证据章节：3、基础实验 [E1]\n\n证据：[E1]",
            [{
                "id": "E1",
                "context": "3、基础实验说明需要把相关文本输入模型。",
            }],
            semantic=True,
        )

        self.assertTrue(report["valid"])
        self.assertEqual(report["semantic_support"]["unsupported_claim_count"], 0)

    def test_validate_answer_citations_can_require_multiple_source_documents(self):
        report = validate_answer_citations(
            "跨资料综合结论。[E1]",
            [
                {"id": "E1", "document_id": "doc-a", "snippet": "资料A支持综合结论。"},
                {"id": "E2", "document_id": "doc-b", "snippet": "资料B也提供相关证据。"},
            ],
            required_source_count=2,
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["reason"], "answer citation source diversity insufficient")
        self.assertEqual(report["cited_source_count"], 1)
        self.assertEqual(report["required_source_count"], 2)

    def test_validate_answer_citations_can_require_source_diversity_per_global_claim(self):
        report = validate_answer_citations(
            "跨资料共同结论只引用资料A。[E1]\n\n资料B的独立补充。[E1][E2]",
            [
                {"id": "E1", "document_id": "doc-a", "snippet": "资料A支持共同结论。"},
                {"id": "E2", "document_id": "doc-b", "snippet": "资料B提供独立补充。"},
            ],
            required_source_count=2,
            require_source_count_per_claim=True,
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["reason"], "answer claim source diversity insufficient")
        self.assertEqual(report["cited_source_count"], 2)
        self.assertEqual(report["insufficient_claim_count"], 1)

    def test_validate_answer_citations_counts_distinct_documents_not_citation_ids(self):
        same_source = validate_answer_citations(
            "结论一。[E1]\n结论二。[E2]",
            [
                {"id": "E1", "document_id": "doc-a", "snippet": "资料支持结论一。"},
                {"id": "E2", "document_id": "doc-a", "snippet": "同一资料支持结论二。"},
            ],
            required_source_count=2,
        )
        distinct_sources = validate_answer_citations(
            "结论一。[E1]\n结论二。[E2]",
            [
                {"id": "E1", "document_id": "doc-a", "snippet": "资料A支持结论一。"},
                {"id": "E2", "document_id": "doc-b", "snippet": "资料B支持结论二。"},
            ],
            required_source_count=2,
        )

        self.assertFalse(same_source["valid"])
        self.assertEqual(same_source["cited_source_count"], 1)
        self.assertTrue(distinct_sources["valid"])
        self.assertEqual(distinct_sources["cited_source_count"], 2)

    def test_validate_answer_citations_does_not_count_visual_evidence_as_document_source(self):
        report = validate_answer_citations(
            "文档结论。[E1]\n本轮画面状态。[V1]",
            [
                {"id": "E1", "document_id": "doc-a", "snippet": "文档支持结论。"},
                {"id": "V1", "modality": "image", "source_filename": "screen-1", "snippet": "画面状态。"},
            ],
            required_source_count=2,
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["cited_source_count"], 1)

    def test_validate_answer_citations_semantic_support_ignores_page_and_chunk_markers(self):
        report = validate_answer_citations(
            "根据证据中的第[19]切片，私有文献详情中的第121页原文支持了结论：需要输入相关文本。[E1]",
            [{
                "id": "E1",
                "context": "需要将与答案相关的文本输入大模型，才能取得更好的效果。",
            }],
            semantic=True,
        )

        self.assertTrue(report["valid"])

    def test_visual_evidence_uses_v_citation_namespace(self):
        gate = evaluate_visual_evidence_gate([
            {
                "id": "V1",
                "modality": "image",
                "confidence": 0.91,
                "context": "柱状图显示本轮准确率为 85%。",
            }
        ])
        report = validate_answer_citations(
            "截图中的柱状图显示准确率为 85%。[V1]",
            [{
                "id": "V1",
                "modality": "image",
                "confidence": 0.91,
                "context": "柱状图显示本轮准确率为 85%。",
            }],
        )

        self.assertTrue(gate.allowed)
        self.assertTrue(report["valid"])
        self.assertEqual(report["used"], ["V1"])

    def test_visual_claim_cannot_be_backed_by_document_evidence(self):
        report = validate_answer_citations(
            "根据本轮截图/视觉识别，证据位于 [E1] 的文档标题《实验报告》中。",
            [{
                "id": "E1",
                "modality": "document",
                "context": "实验报告的文档标题为实验报告。",
            }],
            semantic=True,
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["reason"], "visual citation does not match visual evidence")
        self.assertEqual(
            report["visual_namespace"]["invalid_claims"][0]["issues"],
            ["missing_visual_evidence"],
        )

    def test_v_citation_requires_visual_modality_metadata(self):
        report = validate_answer_citations(
            "本轮准确率为 85%。[V1]",
            [{
                "id": "V1",
                "modality": "document",
                "context": "本轮准确率为 85%。",
            }],
        )

        self.assertFalse(report["valid"])
        self.assertEqual(report["reason"], "visual citation does not match visual evidence")
        self.assertEqual(
            report["visual_namespace"]["invalid_claims"][0]["issues"],
            ["invalid_visual_evidence"],
        )

    def test_visual_evidence_gate_rejects_low_confidence_perception(self):
        gate = evaluate_visual_evidence_gate([
            {
                "id": "V1",
                "modality": "image",
                "confidence": 0.42,
                "context": "模糊文字",
            }
        ])

        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "visual evidence confidence below threshold")

    def test_audio_evidence_uses_a_citation_namespace(self):
        evidence = [{
            "id": "A1",
            "modality": "audio",
            "confidence": 0.85,
            "context": "周五前由小王提交实验报告。",
            "explicit": True,
        }]
        gate = evaluate_audio_evidence_gate(evidence)
        report = validate_answer_citations("行动项：小王在周五前提交实验报告。[A1]", evidence)

        self.assertTrue(gate.allowed)
        self.assertTrue(report["valid"])
        self.assertEqual(report["used"], ["A1"])

    def test_audio_evidence_gate_rejects_unconfirmed_transcript(self):
        gate = evaluate_audio_evidence_gate([{
            "id": "A1",
            "modality": "audio",
            "confidence": 0.99,
            "context": "周五前由小王提交实验报告。",
            "explicit": False,
        }])

        self.assertFalse(gate.allowed)
        self.assertIn("explicitly confirmed", gate.reason)


if __name__ == "__main__":
    unittest.main()
