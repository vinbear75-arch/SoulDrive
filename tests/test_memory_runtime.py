import math
import unittest

from core.memory_runtime import build_memory_capsule, gate_memory_context


class MemoryRuntimeTests(unittest.TestCase):
    def test_project_constraint_is_admitted_without_query_keyword(self):
        result = gate_memory_context(
            "请概览本轮材料。",
            [{
                "memory_id": "mem-project-constraint",
                "subtype": "project_constraint",
                "scope": "project",
                "project_id": "project-a",
                "title": "项目交付约束",
                "content": "本项目必须先完成证据核对，再生成交付物。",
                "score": 0.0,
                "updated_at": 100.0,
            }],
            project_id="project-a",
        )

        self.assertEqual(result["gate"]["accepted_memory_ids"], ["mem-project-constraint"])
        self.assertEqual(result["memories"][0]["context_gate_reason"], "project_constraint")

    def test_unbound_project_constraint_is_rejected_fail_closed(self):
        result = gate_memory_context(
            "项目交付约束",
            [{
                "memory_id": "mem-unbound-constraint",
                "subtype": "project_constraint",
                "scope": "global",
                "title": "项目交付约束",
                "content": "本项目必须先完成证据核对。",
                "score": 1.0,
            }],
        )

        self.assertEqual(result["memories"], [])
        self.assertEqual(
            result["gate"]["rejected"],
            [{"memory_id": "mem-unbound-constraint", "reason": "project_constraint_unbound"}],
        )

    def test_build_memory_capsule_prioritizes_preferences_and_bounds_text(self):
        capsule = build_memory_capsule(
            [
                {
                    "memory_id": "mem_workflow",
                    "subtype": "workflow_fact",
                    "title": "Workflow",
                    "content": "Use the project branch for implementation.",
                    "updated_at": 1.0,
                },
                {
                    "memory_id": "mem_pref",
                    "subtype": "user_preference",
                    "title": "Review preference",
                    "content": "User wants human review before long-term memory writes.",
                    "updated_at": 2.0,
                },
            ],
            max_chars=120,
        )

        self.assertEqual(capsule["items"][0]["memory_id"], "mem_pref")
        self.assertLessEqual(len(capsule["text"]), 120)
        self.assertIn("Review preference", capsule["text"])

    def test_build_memory_capsule_keeps_provenance_metadata_without_source_excerpt(self):
        capsule = build_memory_capsule(
            [
                {
                    "memory_id": "mem_project_fact",
                    "subtype": "project_fact",
                    "scope": "project",
                    "project_id": "project-a",
                    "valid_from": 100.0,
                    "invalid_at": 200.0,
                    "source_session_id": "session-source-a",
                    "source_message_ids": ["msg-source-a"],
                    "source_message_sequences": [7],
                    "source_message_indexes": [4, 5],
                    "source_excerpt": "不应进入记忆胶囊的原始对话片段。",
                    "title": "审计范围",
                    "content": "本项目只允许本地审计。",
                    "updated_at": 3.0,
                },
            ],
            max_chars=400,
        )

        self.assertIn("memory_id=mem_project_fact", capsule["text"])
        self.assertIn("scope=project:project-a", capsule["text"])
        self.assertIn("valid_from=100", capsule["text"])
        self.assertIn("invalid_at=200", capsule["text"])
        self.assertIn("source_session=session-source-a", capsule["text"])
        self.assertIn("source_message_ids=msg-source-a", capsule["text"])
        self.assertIn("source_sequences=7", capsule["text"])
        self.assertIn("source_messages=4,5", capsule["text"])
        self.assertNotIn("原始对话片段", capsule["text"])

    def test_build_memory_capsule_shows_workflow_binding(self):
        capsule = build_memory_capsule(
            [{
                "memory_id": "mem-paper-workflow",
                "subtype": "workflow_fact",
                "workflow_id": "paper_review",
                "title": "论文窗口规则",
                "content": "先核对章节证据。",
            }],
            max_chars=200,
        )

        self.assertIn("workflow=paper_review", capsule["text"])

    def test_build_memory_capsule_preserves_provenance_when_content_is_truncated(self):
        capsule = build_memory_capsule(
            [
                {
                    "memory_id": "mem-trace-preserve",
                    "subtype": "project_fact",
                    "scope": "project",
                    "project_id": "project-a",
                    "source_session_id": "session-source-a",
                    "source_message_ids": ["msg-source-a"],
                    "title": "审计范围",
                    "content": "这是很长的正文，用于验证在上下文预算不足时仍然保留来源标识和项目范围。" * 8,
                    "updated_at": 3.0,
                },
            ],
            max_chars=150,
        )

        self.assertLessEqual(len(capsule["text"]), 150)
        self.assertIn("memory_id=mem-trace-preserve", capsule["text"])
        self.assertIn("scope=project:project-a", capsule["text"])
        self.assertIn("source_message_ids=msg-source-a", capsule["text"])

    def test_gate_memory_context_uses_stable_memory_id_tie_breaker(self):
        memories = [
            {
                "memory_id": "mem-a",
                "subtype": "project_fact",
                "title": "project policy A",
                "content": "project policy uses local models",
                "score": 0.5,
                "updated_at": 100.0,
            },
            {
                "memory_id": "mem-b",
                "subtype": "project_fact",
                "title": "project policy B",
                "content": "project policy uses local models",
                "score": 0.5,
                "updated_at": 100.0,
            },
        ]

        reversed_result = gate_memory_context(
            "project policy",
            list(reversed(memories)),
            max_items=1,
        )
        normal_result = gate_memory_context(
            "project policy",
            memories,
            max_items=1,
        )

        self.assertEqual(reversed_result["gate"]["accepted_memory_ids"], ["mem-a"])
        self.assertEqual(normal_result["gate"]["accepted_memory_ids"], ["mem-a"])

    def test_gate_memory_context_deduplicates_repeated_memory_id(self):
        result = gate_memory_context(
            "项目策略",
            [
                {
                    "memory_id": "mem-duplicate",
                    "subtype": "project_fact",
                    "title": "项目策略",
                    "content": "项目策略：使用本地模型。",
                    "score": 0.9,
                    "record_version": 2,
                    "updated_at": 200.0,
                },
                {
                    "memory_id": "mem-duplicate",
                    "subtype": "project_fact",
                    "title": "项目策略",
                    "content": "项目策略：使用本地模型。",
                    "score": 0.8,
                    "record_version": 1,
                    "updated_at": 100.0,
                },
            ],
        )

        self.assertEqual(result["gate"]["accepted_memory_ids"], ["mem-duplicate"])
        self.assertEqual(len(result["memory_capsule"]["items"]), 1)
        self.assertIn(
            {"memory_id": "mem-duplicate", "reason": "duplicate_memory_id", "conflict_with": "mem-duplicate"},
            result["gate"]["rejected"],
        )

    def test_historical_memory_gate_uses_reference_time_for_freshness(self):
        result = gate_memory_context(
            "项目策略",
            [{
                "memory_id": "mem-historical",
                "subtype": "project_fact",
                "title": "项目策略",
                "content": "项目策略：使用本地模型。",
                "score": 0.1,
                "confirmed_at": 100.0,
                "updated_at": 100.0,
            }],
            reference_timestamp=110.0,
        )

        self.assertEqual(result["gate"]["accepted_memory_ids"], ["mem-historical"])
        self.assertEqual(result["memories"][0]["freshness_weight"], 1.0)

    def test_historical_memory_gate_ignores_future_access_timestamp(self):
        result = gate_memory_context(
            "项目策略",
            [{
                "memory_id": "mem-future-use",
                "subtype": "project_fact",
                "title": "项目策略",
                "content": "项目策略：使用本地模型。",
                "score": 0.1,
                "confirmed_at": 100.0,
                "last_used_at": 4_000_000.0,
                "updated_at": 100.0,
            }],
            reference_timestamp=3_000_000.0,
        )

        self.assertEqual(result["gate"]["accepted_memory_ids"], ["mem-future-use"])
        self.assertEqual(result["memories"][0]["freshness_weight"], 0.85)

    def test_gate_rejects_memories_outside_reference_time(self):
        result = gate_memory_context(
            "项目策略",
            [
                {
                    "memory_id": "mem-not-yet-valid",
                    "subtype": "project_fact",
                    "title": "项目策略",
                    "content": "项目策略：未来才生效。",
                    "score": 1.0,
                    "valid_from": 200.0,
                    "updated_at": 100.0,
                },
                {
                    "memory_id": "mem-expired",
                    "subtype": "project_fact",
                    "title": "项目策略",
                    "content": "项目策略：已经过期。",
                    "score": 1.0,
                    "invalid_at": 100.0,
                    "updated_at": 90.0,
                },
            ],
            reference_timestamp=150.0,
        )

        self.assertEqual(result["memories"], [])
        self.assertEqual(
            result["gate"]["rejected"],
            [
                {"memory_id": "mem-not-yet-valid", "reason": "memory_not_yet_valid"},
                {"memory_id": "mem-expired", "reason": "memory_expired"},
            ],
        )

    def test_gate_rejects_malformed_memory_validity(self):
        result = gate_memory_context(
            "项目策略",
            [{
                "memory_id": "mem-invalid-interval",
                "subtype": "project_fact",
                "title": "项目策略",
                "content": "项目策略：时间字段损坏。",
                "score": 1.0,
                "valid_from": "not-a-timestamp",
                "updated_at": 100.0,
            }],
            reference_timestamp=150.0,
        )

        self.assertEqual(result["memories"], [])
        self.assertEqual(
            result["gate"]["rejected"],
            [{"memory_id": "mem-invalid-interval", "reason": "invalid_memory_validity"}],
        )

    def test_gate_rejects_non_finite_retrieval_scores_for_irrelevant_memory(self):
        result = gate_memory_context(
            "项目策略",
            [
                {
                    "memory_id": "mem-infinite-score",
                    "subtype": "project_fact",
                    "title": "无关事实",
                    "content": "这条记忆只记录蓝色标签。",
                    "score": math.inf,
                    "updated_at": 100.0,
                },
                {
                    "memory_id": "mem-nan-score",
                    "subtype": "project_fact",
                    "title": "另一条无关事实",
                    "content": "这条记忆只记录绿色标签。",
                    "score": math.nan,
                    "updated_at": 99.0,
                },
            ],
            reference_timestamp=150.0,
        )

        self.assertEqual(result["memories"], [])
        self.assertEqual(
            result["gate"]["rejected"],
            [
                {"memory_id": "mem-infinite-score", "reason": "query_irrelevant"},
                {"memory_id": "mem-nan-score", "reason": "query_irrelevant"},
            ],
        )

    def test_gate_requires_workflow_namespace_for_workflow_memory(self):
        result = gate_memory_context(
            "研究窗口规则",
            [{
                "memory_id": "mem-paper-workflow",
                "subtype": "workflow_fact",
                "workflow_id": "paper_review",
                "title": "研究窗口规则",
                "content": "论文窗口只引用章节证据。",
                "score": 1.0,
            }],
        )

        self.assertEqual(result["memories"], [])
        self.assertEqual(
            result["gate"]["rejected"],
            [{
                "memory_id": "mem-paper-workflow",
                "reason": "workflow_memory_namespace_required",
            }],
        )

    def test_gate_rejects_workflow_memory_from_another_namespace(self):
        result = gate_memory_context(
            "研究窗口规则",
            [{
                "memory_id": "mem-paper-workflow",
                "subtype": "workflow_fact",
                "workflow_id": "paper_review",
                "title": "研究窗口规则",
                "content": "论文窗口只引用章节证据。",
                "score": 1.0,
            }],
            workflow_id="meeting_actions",
        )

        self.assertEqual(result["memories"], [])
        self.assertEqual(
            result["gate"]["rejected"],
            [{
                "memory_id": "mem-paper-workflow",
                "reason": "workflow_memory_namespace_mismatch",
            }],
        )

    def test_gate_rejects_unknown_workflow_memory_namespace(self):
        result = gate_memory_context(
            "研究窗口规则",
            [{
                "memory_id": "mem-unknown-workflow",
                "subtype": "workflow_fact",
                "workflow_id": "unknown_window",
                "title": "未知窗口规则",
                "content": "未知窗口不能进入正式上下文。",
                "score": 1.0,
            }],
            workflow_id="paper_review",
        )

        self.assertEqual(result["memories"], [])
        self.assertEqual(
            result["gate"]["rejected"],
            [{
                "memory_id": "mem-unknown-workflow",
                "reason": "workflow_memory_namespace_unknown",
            }],
        )

    def test_gate_rejects_project_memory_without_project_context(self):
        result = gate_memory_context(
            "项目资料范围",
            [{
                "memory_id": "mem-project-a",
                "subtype": "project_fact",
                "scope": "project",
                "project_id": "project-a",
                "title": "项目资料范围",
                "content": "项目 A 只使用已核验资料。",
                "score": 1.0,
            }],
        )

        self.assertEqual(result["memories"], [])
        self.assertEqual(
            result["gate"]["rejected"],
            [{"memory_id": "mem-project-a", "reason": "project_memory_scope_required"}],
        )

    def test_gate_rejects_project_memory_from_another_project(self):
        result = gate_memory_context(
            "项目资料范围",
            [{
                "memory_id": "mem-project-a",
                "subtype": "project_fact",
                "scope": "project",
                "project_id": "project-a",
                "title": "项目资料范围",
                "content": "项目 A 只使用已核验资料。",
                "score": 1.0,
            }],
            project_id="project-b",
        )

        self.assertEqual(result["memories"], [])
        self.assertEqual(
            result["gate"]["rejected"],
            [{"memory_id": "mem-project-a", "reason": "project_memory_scope_mismatch"}],
        )

    def test_gate_rejects_global_memory_when_global_scope_is_disabled(self):
        result = gate_memory_context(
            "项目策略",
            [{
                "memory_id": "mem-global",
                "subtype": "project_fact",
                "scope": "global",
                "title": "项目策略",
                "content": "全局事实不应进入项目专属读取。",
                "score": 1.0,
            }],
            project_id="project-a",
            include_global=False,
        )

        self.assertEqual(result["memories"], [])
        self.assertEqual(
            result["gate"]["rejected"],
            [{"memory_id": "mem-global", "reason": "global_memory_scope_disabled"}],
        )

    def test_gate_memory_context_emits_stable_decision_digest_without_plaintext(self):
        memories = [
            {
                "memory_id": "mem-policy-a",
                "subtype": "project_fact",
                "title": "项目策略",
                "content": "项目策略：使用本地模型。",
                "score": 0.9,
                "updated_at": 100.0,
            },
            {
                "memory_id": "mem-legacy-b",
                "subtype": "legacy_note",
                "title": "旧记录",
                "content": "旧记录正文不应进入上下文。",
                "score": 0.8,
                "updated_at": 100.0,
            },
        ]

        normal_result = gate_memory_context(
            "项目策略",
            memories,
            reference_timestamp=1_000.0,
        )
        reversed_result = gate_memory_context(
            "项目策略",
            list(reversed(memories)),
            reference_timestamp=1_000.0,
        )

        normal_digest = normal_result["gate"]["decision_digest"]
        self.assertEqual(normal_digest, reversed_result["gate"]["decision_digest"])
        self.assertRegex(normal_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(
            normal_result["gate"]["prompt_binding_digest"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertNotIn("项目策略", normal_result["gate"])
        self.assertNotIn("旧记录正文", normal_result["gate"])

    def test_gate_memory_context_changes_decision_digest_when_admission_changes(self):
        base_memory = {
            "memory_id": "mem-policy-a",
            "subtype": "project_fact",
            "title": "项目策略",
            "content": "项目策略：使用本地模型。",
            "score": 0.9,
            "updated_at": 100.0,
        }
        base_result = gate_memory_context(
            "项目策略",
            [base_memory],
            reference_timestamp=1_000.0,
        )
        expanded_result = gate_memory_context(
            "项目策略",
            [
                base_memory,
                {
                    "memory_id": "mem-policy-b",
                    "subtype": "project_fact",
                    "title": "项目策略补充",
                    "content": "项目策略补充：保留来源证据。",
                    "score": 0.8,
                    "updated_at": 99.0,
                },
            ],
            reference_timestamp=1_000.0,
        )

        self.assertNotEqual(
            base_result["gate"]["decision_digest"],
            expanded_result["gate"]["decision_digest"],
        )


if __name__ == "__main__":
    unittest.main()
