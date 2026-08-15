import unittest

from core.parent_child_index import split_parent_document


class ParentChildIndexTests(unittest.TestCase):
    def test_split_parent_document_preserves_parent_id_and_offsets(self):
        text = "A" * 120 + "B" * 120 + "C" * 120

        children = split_parent_document(
            parent_id="paper-parent-1",
            text=text,
            child_size=140,
            child_overlap=20,
        )

        self.assertEqual(children[0].parent_id, "paper-parent-1")
        self.assertEqual(children[0].start_char, 0)
        self.assertGreater(len(children), 1)
        self.assertLess(children[1].start_char, children[0].end_char)

    def test_split_parent_document_prefers_paragraph_boundaries_with_overlap(self):
        text = "\n\n".join(
            [
                "# 一、总体要求",
                "第一段说明项目目标、适用范围和实施边界。",
                "第二段说明系统架构、数据流和权限控制。",
                "第三段说明验收指标、风险控制和运维要求。",
                "第四段说明后续扩展、表格附件和流程图说明。",
            ]
        )

        children = split_parent_document(
            parent_id="scheme-parent-1",
            text=text,
            child_size=55,
            child_overlap=15,
        )

        self.assertGreater(len(children), 1)
        self.assertTrue(all(child.chunk_strategy == "dynamic_structure_overlap_v1" for child in children))
        self.assertTrue(all(child.content.strip() for child in children))
        self.assertGreater(children[1].overlap_chars, 0)
        self.assertIn("第二段", children[0].content)
        self.assertIn("第二段", children[1].content)

    def test_split_parent_document_keeps_markdown_table_together(self):
        text = "\n\n".join(
            [
                "实施范围如下。",
                "| 阶段 | 产物 |\n|---|---|\n| 调研 | 需求说明 |\n| 建设 | 技术方案 |",
                "表格之后说明验收要求。",
            ]
        )

        children = split_parent_document(
            parent_id="table-parent-1",
            text=text,
            child_size=140,
            child_overlap=10,
        )

        table_children = [child for child in children if "| 阶段 | 产物 |" in child.content]
        self.assertEqual(len(table_children), 1)
        self.assertIn("| 建设 | 技术方案 |", table_children[0].content)

    def test_split_parent_document_splits_large_table_and_repeats_header(self):
        rows = [f"| 阶段{i} | 交付物{i} | 验收标准{i} |" for i in range(30)]
        table = "\n".join([
            "| 阶段 | 交付物 | 验收标准 |",
            "| --- | --- | --- |",
            *rows,
        ])

        children = split_parent_document(
            parent_id="large-table",
            text=table,
            child_size=260,
            child_overlap=40,
        )

        self.assertGreater(len(children), 1)
        self.assertTrue(all(child.content.startswith("| 阶段 | 交付物 | 验收标准 |") for child in children))
        self.assertTrue(all("| --- | --- | --- |" in child.content for child in children))
        combined = "\n".join(child.content for child in children)
        self.assertIn("| 阶段0 | 交付物0 | 验收标准0 |", combined)
        self.assertIn("| 阶段29 | 交付物29 | 验收标准29 |", combined)

    def test_formal_structure_uses_two_paragraph_window_with_one_paragraph_overlap(self):
        paragraphs = [
            "第一段明确项目建设目标和适用范围。",
            "第二段规定总体架构和数据治理要求。",
            "第三段规定安全控制和权限管理要求。",
            "第四段规定验收指标和运行维护要求。",
        ]
        text = "\n\n".join(["# 第一章 总体要求", *paragraphs])

        children = split_parent_document(
            parent_id="formal-policy",
            text=text,
            child_size=900,
            child_overlap=120,
            formal_structure=True,
        )

        self.assertEqual(len(children), 3)
        self.assertEqual(
            [child.semantic_unit_count for child in children],
            [2, 2, 2],
        )
        self.assertIn(paragraphs[1], children[0].content)
        self.assertIn(paragraphs[1], children[1].content)
        self.assertIn(paragraphs[2], children[1].content)
        self.assertIn(paragraphs[2], children[2].content)
        self.assertTrue(
            all(child.chunk_strategy == "formal_structure_paragraph_window_v2" for child in children)
        )

    def test_heading_only_child_counts_as_structural_semantic_unit(self):
        children = split_parent_document(
            parent_id="heading-only",
            text="# Appendix A",
            formal_structure=True,
        )

        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].semantic_unit_count, 1)

    def test_token_limit_prevents_embedding_truncation(self):
        text = "\n\n".join(
            [
                "第一条 " + "甲" * 180,
                "第二条 " + "乙" * 180,
                "第三条 " + "丙" * 180,
            ]
        )

        children = split_parent_document(
            parent_id="token-safe-policy",
            text=text,
            child_size=900,
            child_overlap=30,
            formal_structure=True,
            token_counter=len,
            max_tokens=120,
        )

        self.assertGreater(len(children), 3)
        self.assertTrue(all(child.token_count <= 120 for child in children))
        self.assertIn("甲", "".join(child.content for child in children))
        self.assertIn("丙", "".join(child.content for child in children))

    def test_heading_and_body_without_blank_line_still_respect_token_limit(self):
        text = "\n".join(
            [
                "# 1 INTRODUCTION",
                "Author One, Author Two",
                "Retrieval augmented generation improves factual accuracy. " * 20,
                "The final paragraph explains evaluation and deployment risks. " * 12,
            ]
        )

        children = split_parent_document(
            parent_id="docling-heading-body",
            text=text,
            formal_structure=True,
            token_counter=len,
            max_tokens=140,
        )

        self.assertGreater(len(children), 4)
        self.assertTrue(all(child.token_count <= 140 for child in children))
        combined = "".join(child.content for child in children)
        self.assertIn("# 1 INTRODUCTION", combined)
        self.assertIn("deployment risks", combined)

    def test_formal_structure_keeps_table_separate_from_prose_windows(self):
        table = "| 指标 | 要求 |\n| --- | --- |\n| 可用性 | 99.9% |"
        text = "\n\n".join(
            [
                "第一段说明建设目标。",
                "第二段说明总体架构。",
                table,
                "第三段说明验收方式。",
                "第四段说明运维要求。",
            ]
        )

        children = split_parent_document(
            parent_id="formal-table",
            text=text,
            formal_structure=True,
        )

        table_children = [child for child in children if "| 指标 | 要求 |" in child.content]
        self.assertEqual(len(table_children), 1)
        self.assertNotIn("第二段", table_children[0].content)
        self.assertNotIn("第三段", table_children[0].content)

    def test_wide_table_rows_do_not_exceed_embedding_token_limit(self):
        table = "\n".join(
            [
                "| 指标名称 | 详细验收要求 |",
                "| --- | --- |",
                "| 完整性 | 不得缺项 |",
                "| 安全性 | " + "必须满足访问控制要求" * 12 + " |",
                "| 可用性 | " + "必须满足连续运行要求" * 12 + " |",
            ]
        )

        children = split_parent_document(
            parent_id="wide-table",
            text=table,
            formal_structure=True,
            token_counter=len,
            max_tokens=80,
        )

        self.assertGreater(len(children), 2)
        self.assertTrue(all(child.token_count <= 80 for child in children))
        combined = "".join(child.content for child in children)
        self.assertIn("访问控制要求", combined)
        self.assertIn("连续运行要求", combined)

    def test_two_line_wide_table_is_forced_below_embedding_token_limit(self):
        table = "\n".join(
            [
                "| 指标名称 | 详细验收要求 |",
                "| 安全性 | " + "必须满足访问控制、审计和应急响应要求" * 20 + " |",
            ]
        )

        children = split_parent_document(
            parent_id="two-line-wide-table",
            text=table,
            formal_structure=True,
            token_counter=len,
            max_tokens=90,
        )

        self.assertGreater(len(children), 2)
        self.assertTrue(all(child.token_count <= 90 for child in children))
        self.assertIn("应急响应要求", "".join(child.content for child in children))


if __name__ == "__main__":
    unittest.main()
