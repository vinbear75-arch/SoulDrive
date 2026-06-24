import unittest

from core.rag_engine import build_answer_mindmap, query_requests_mindmap


class RagMindmapTests(unittest.TestCase):
    def test_answer_mindmap_maps_answer_points_to_l2_branches(self):
        query = "生成式人工智能服务安全基本要求主要覆盖哪些方面？"
        answer = (
            "主要包括以下几个方面：\n"
            "1. 语料安全：训练数据质量、安全性和合规性是基础。[E1]\n"
            "   - 数据来源要可追溯。\n"
            "   - 敏感内容要清洗。\n"
            "2. 模型安全：重点关注对抗攻击、越狱和提示注入。[E2]\n"
            "3. 安全措施：为服务提供者设置适用人群、场景和用途边界。[E3]\n"
            "4. 安全评估：覆盖上线前评估和持续监测。[E1]"
        )

        mindmap = build_answer_mindmap(query, answer)

        self.assertTrue(query_requests_mindmap("生成式人工智能服务安全基本要求主要覆盖哪些方面？以导图形式展示"))
        self.assertIn("# 生成式人工智能服务安全要求", mindmap)
        self.assertNotIn(f"# {query}", mindmap)
        self.assertIn("## 语料安全", mindmap)
        self.assertIn("## 模型安全", mindmap)
        self.assertIn("## 安全措施", mindmap)
        self.assertIn("## 安全评估", mindmap)
        self.assertIn("数据来源要可追溯", mindmap)
        self.assertIn("敏感内容要清洗", mindmap)
        self.assertNotIn("问题焦点", mindmap)
        self.assertNotIn("论文依据", mindmap)
        self.assertNotIn("[E1]", mindmap)
        self.assertNotIn("[E2]", mindmap)
        self.assertNotIn("[E3]", mindmap)

    def test_answer_mindmap_falls_back_to_full_core_answer_when_not_listed(self):
        query = "请用导图说明为什么需要安全评估"
        answer = (
            "安全评估之所以重要，是因为它能把上线前验证和持续监测串起来，避免只看一次性的结果。"
            "同时它还可以把发现的问题反馈到整改、优化和持续改进流程中，"
            "让服务提供者在真实运行阶段持续识别风险、更新控制措施并保留可追踪的治理依据。[E3]"
        )

        mindmap = build_answer_mindmap(query, answer)

        self.assertIn("# 安全评估必要性", mindmap)
        self.assertNotIn(f"# {query}", mindmap)
        self.assertIn("## 核心回答", mindmap)
        self.assertIn("安全评估之所以重要", mindmap)
        self.assertIn("保留可追踪的治理依据", mindmap)
        self.assertNotIn("…", mindmap)
        self.assertNotIn("[E3]", mindmap)
        self.assertNotIn("## 论文依据", mindmap)
        self.assertNotIn("问题焦点", mindmap)

    def test_answer_mindmap_uses_question_topic_and_keeps_final_summary_as_l2(self):
        query = "数据要素发展报告中的“供得出、流得动、用得好、保安全”分别指向什么治理目标？请以导图的形式加以展示"
        answer = (
            "根据报告内容，可以将四个目标归纳如下：\n"
            "- “供得出” 指向数据供给的增加和提升。这需要通过制度建设来保障高质量的数据供应。[E2]\n"
            "- “流得动” 指的是数据流通顺畅，即解决数据跨主体复用时可能产生的矛盾与摩擦问题。[E2]\n"
            "- “用得好” 强调的是高效利用数据，通过技术创新和产业实践实现深度应用。[E2]\n"
            "- “保安全” 是指保障数据的安全性，确保数据在流通使用过程中不被泄露或滥用。[E2]\n"
            "- 虽然用户请求以导图形式展示，但基于当前文本信息的限制，上述归纳可以作为理解四个目标的基础框架。"
        )

        mindmap = build_answer_mindmap(query, answer)

        self.assertIn("# 数据要素治理目标", mindmap)
        self.assertNotIn("# " + query, mindmap)
        self.assertNotIn("请以导图的形式加以展示", mindmap.splitlines()[0])
        self.assertIn("## “供得出” 指向数据供给的增加和提升", mindmap)
        self.assertIn("## “流得动” 指的是数据流通顺畅，即解决数据跨主体复用时可能产生的矛盾与摩擦问题", mindmap)
        self.assertIn("## “用得好” 强调的是高效利用数据，通过技术创新和产业实践实现深度应用", mindmap)
        self.assertIn("## “保安全” 是指保障数据的安全性，确保数据在流通使用过程中不被泄露或滥用", mindmap)
        self.assertIn("## 归纳总结", mindmap)
        self.assertIn("虽然用户请求以导图形式展示", mindmap)
        self.assertNotRegex(mindmap, r"## “保安全”[\s\S]*\n\s+- 虽然用户请求以导图形式展示")


if __name__ == "__main__":
    unittest.main()
