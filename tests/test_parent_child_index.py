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
            child_size=45,
            child_overlap=10,
        )

        table_children = [child for child in children if "| 阶段 | 产物 |" in child.content]
        self.assertEqual(len(table_children), 1)
        self.assertIn("| 建设 | 技术方案 |", table_children[0].content)


if __name__ == "__main__":
    unittest.main()
