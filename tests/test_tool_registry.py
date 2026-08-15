import unittest

from core.tool_registry import ToolRegistry, ToolSpec, build_default_tool_registry


class ToolRegistryTests(unittest.TestCase):
    def test_default_registry_can_include_audit_verify_tool(self):
        registry = build_default_tool_registry(
            runtime_status_handler=lambda _arguments: {"locked": False},
            audit_recent_handler=lambda _arguments: {"events": []},
            workspace_diagnostics_handler=lambda _arguments: {"ready": True},
            product_diagnostics_handler=lambda _arguments: {"ready": True},
            audit_verify_handler=lambda _arguments: {"ready": True},
        )

        names = {tool["name"] for tool in registry.list_tools()}
        result = registry.call("souldrive.audit_verify", {"limit": 10})

        self.assertEqual(names, {
            "souldrive.audit_recent",
            "souldrive.audit_verify",
            "souldrive.product_diagnostics",
            "souldrive.runtime_status",
            "souldrive.workspace_diagnostics",
        })
        self.assertEqual(result, {"ready": True})

        audit_tool = next(tool for tool in registry.list_tools() if tool["name"] == "souldrive.audit_verify")
        self.assertEqual(audit_tool["access_level"], "read")
        self.assertFalse(audit_tool["mutates_state"])
        self.assertTrue(audit_tool["requires_unlocked_workspace"])

    def test_registry_rejects_out_of_bounds_arguments(self):
        registry = build_default_tool_registry(
            runtime_status_handler=lambda _arguments: {"locked": False},
            audit_recent_handler=lambda arguments: {"limit": arguments["limit"]},
            workspace_diagnostics_handler=lambda _arguments: {"ready": True},
        )

        with self.assertRaises(ValueError):
            registry.call("souldrive.audit_recent", {"limit": 1000})

    def test_registry_rejects_unknown_arguments_when_schema_disallows_them(self):
        registry = build_default_tool_registry(
            runtime_status_handler=lambda _arguments: {"locked": False},
            audit_recent_handler=lambda _arguments: {"events": []},
            workspace_diagnostics_handler=lambda _arguments: {"ready": True},
        )

        with self.assertRaises(ValueError):
            registry.call("souldrive.runtime_status", {"unexpected": True})

    def test_registry_requires_mutating_tools_to_declare_non_read_access(self):
        registry = ToolRegistry()

        with self.assertRaises(ValueError):
            registry.register(
                ToolSpec(
                    name="bad.write",
                    description="Bad mutating tool.",
                    input_schema={"type": "object", "properties": {}},
                    handler=lambda _arguments: {},
                    mutates_state=True,
                )
            )

    def test_default_registry_can_generate_presentation_task(self):
        registry = build_default_tool_registry(
            runtime_status_handler=lambda _arguments: {"locked": False},
            audit_recent_handler=lambda _arguments: {"events": []},
            workspace_diagnostics_handler=lambda _arguments: {"ready": True},
            presentation_generate_handler=lambda arguments: {
                "task_id": "task-1",
                "sources": arguments["source_filenames"],
            },
        )

        result = registry.call(
            "souldrive.presentation_generate",
            {
                "source_filenames": ["方案.pdf"],
                "audience": "management",
                "max_slides": 8,
                "open_in_wps": False,
            },
        )
        spec = next(item for item in registry.list_tools() if item["name"] == "souldrive.presentation_generate")

        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(spec["access_level"], "write")
        self.assertTrue(spec["mutates_state"])
        self.assertTrue(spec["requires_unlocked_workspace"])


if __name__ == "__main__":
    unittest.main()
