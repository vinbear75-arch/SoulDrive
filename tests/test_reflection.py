import unittest

from core.reflection import build_reflection_candidates, build_reflection_prompt, parse_reflection_response


class ReflectionTests(unittest.TestCase):
    def test_user_preference_candidate_requires_user_evidence(self):
        messages = [
            {"role": "user", "content": "我不希望每次问答后都弹窗保存记忆，希望只在关闭前集中审核。"},
            {"role": "assistant", "content": "明白。"},
        ]

        candidates = build_reflection_candidates(messages)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["asset_type"], "memory")
        self.assertEqual(candidates[0]["subtype"], "user_preference")
        self.assertIn(0, candidates[0]["source_message_indexes"])
        self.assertTrue(candidates[0]["evidence"])

    def test_assistant_only_claim_does_not_create_memory(self):
        messages = [
            {"role": "assistant", "content": "用户喜欢自动保存所有信息。"},
        ]

        self.assertEqual(build_reflection_candidates(messages), [])

    def test_parse_model_json_rejects_candidate_without_evidence(self):
        raw = (
            '{"candidates":[{"asset_type":"memory","subtype":"user_preference",'
            '"title":"偏好","content":"内容","evidence":[],"rationale":"理由",'
            '"confidence":0.9,"selected_by_default":true,"source_message_indexes":[0]}]}'
        )

        self.assertEqual(parse_reflection_response(raw, [{"role": "user", "content": "hello"}]), [])

    def test_build_reflection_prompt_mentions_transcript_and_json(self):
        prompt = build_reflection_prompt(
            [
                {"role": "user", "content": "关闭前集中审核。"},
                {"role": "assistant", "content": "知道了。"},
            ]
        )

        self.assertEqual(prompt[0]["role"], "system")
        self.assertEqual(prompt[1]["role"], "user")
        self.assertIn("<transcript>", prompt[1]["content"])
        self.assertIn('{"candidates"', prompt[0]["content"])


if __name__ == "__main__":
    unittest.main()
