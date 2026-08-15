from dataclasses import dataclass
from typing import Any, Callable


VALID_ACCESS_LEVELS = {"read", "write", "dangerous"}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    access_level: str = "read"
    mutates_state: bool = False
    requires_unlocked_workspace: bool = False

    def public_spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "access_level": self.access_level,
            "mutates_state": self.mutates_state,
            "requires_unlocked_workspace": self.requires_unlocked_workspace,
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec):
        if spec.access_level not in VALID_ACCESS_LEVELS:
            raise ValueError(f"unsupported tool access level: {spec.access_level}")
        if spec.mutates_state and spec.access_level == "read":
            raise ValueError("mutating tools must not use read access level")
        self._tools[spec.name] = spec

    def list_tools(self) -> list[dict[str, Any]]:
        return [spec.public_spec() for spec in sorted(self._tools.values(), key=lambda item: item.name)]

    def call(self, name: str, arguments: dict[str, Any] | None = None):
        spec, validated_arguments = self.validate_call(name, arguments)
        return spec.handler(validated_arguments)

    def validate_call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[ToolSpec, dict[str, Any]]:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        spec = self._tools[name]
        validated_arguments = validate_tool_arguments(spec.input_schema, arguments or {})
        return spec, validated_arguments


def validate_tool_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")

    schema = schema or {}
    if schema.get("type", "object") != "object":
        raise ValueError("tool input_schema must describe an object")

    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    for key in required:
        if key not in arguments:
            raise ValueError(f"missing required tool argument: {key}")

    additional_properties = schema.get("additionalProperties", True)
    if additional_properties is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ValueError(f"unknown tool argument: {unknown[0]}")

    validated = dict(arguments)
    for key, value in validated.items():
        if key not in properties:
            continue
        _validate_property(key, value, properties[key])
    return validated


def _validate_property(key: str, value: Any, schema: dict[str, Any]) -> None:
    expected_type = schema.get("type")
    if expected_type == "integer" and not (isinstance(value, int) and not isinstance(value, bool)):
        raise ValueError(f"tool argument {key} must be an integer")
    if expected_type == "number" and not isinstance(value, (int, float)):
        raise ValueError(f"tool argument {key} must be a number")
    if expected_type == "string" and not isinstance(value, str):
        raise ValueError(f"tool argument {key} must be a string")
    if expected_type == "boolean" and not isinstance(value, bool):
        raise ValueError(f"tool argument {key} must be a boolean")
    if expected_type == "array" and not isinstance(value, list):
        raise ValueError(f"tool argument {key} must be an array")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if minimum_items is not None and len(value) < int(minimum_items):
            raise ValueError(f"tool argument {key} requires at least {minimum_items} items")
        if maximum_items is not None and len(value) > int(maximum_items):
            raise ValueError(f"tool argument {key} exceeds maxItems {maximum_items}")
        item_schema = schema.get("items") or {}
        for index, item in enumerate(value):
            _validate_property(f"{key}[{index}]", item, item_schema)

    allowed_values = schema.get("enum")
    if allowed_values is not None and value not in allowed_values:
        raise ValueError(f"tool argument {key} must be one of {allowed_values}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise ValueError(f"tool argument {key} must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise ValueError(f"tool argument {key} must be <= {maximum}")

    max_length = schema.get("maxLength")
    if max_length is not None and isinstance(value, str) and len(value) > int(max_length):
        raise ValueError(f"tool argument {key} exceeds maxLength {max_length}")


def build_default_tool_registry(
    runtime_status_handler: Callable[[dict[str, Any]], Any],
    audit_recent_handler: Callable[[dict[str, Any]], Any],
    workspace_diagnostics_handler: Callable[[dict[str, Any]], Any],
    product_diagnostics_handler: Callable[[dict[str, Any]], Any] | None = None,
    audit_verify_handler: Callable[[dict[str, Any]], Any] | None = None,
    presentation_generate_handler: Callable[[dict[str, Any]], Any] | None = None,
):
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="souldrive.runtime_status",
        description="Return the current redacted SoulDrive runtime state.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=runtime_status_handler,
    ))
    registry.register(ToolSpec(
        name="souldrive.audit_recent",
        description="Return recent hash-chain audit events.",
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "additionalProperties": False,
        },
        handler=audit_recent_handler,
        requires_unlocked_workspace=True,
    ))
    if audit_verify_handler is not None:
        registry.register(ToolSpec(
            name="souldrive.audit_verify",
            description="Verify the local hash-chain audit log and return tamper-evidence status.",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 10000}},
                "additionalProperties": False,
            },
            handler=audit_verify_handler,
            requires_unlocked_workspace=True,
        ))
    registry.register(ToolSpec(
        name="souldrive.workspace_diagnostics",
        description="Return local workspace layout diagnostics.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=workspace_diagnostics_handler,
    ))
    if product_diagnostics_handler is not None:
        registry.register(ToolSpec(
            name="souldrive.product_diagnostics",
            description="Return local runtime diagnostics for SoulDrive.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=product_diagnostics_handler,
        ))
    if presentation_generate_handler is not None:
        registry.register(ToolSpec(
            name="souldrive.presentation_generate",
            description="Generate an editable PPTX from selected indexed documents and optionally open it in WPS.",
            input_schema={
                "type": "object",
                "properties": {
                    "source_filenames": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 260},
                        "minItems": 1,
                        "maxItems": 12,
                    },
                    "title": {"type": "string", "maxLength": 120},
                    "audience": {"type": "string", "enum": ["management", "technical", "government", "general"]},
                    "max_slides": {"type": "integer", "minimum": 4, "maximum": 16},
                    "open_in_wps": {"type": "boolean"},
                },
                "required": ["source_filenames"],
                "additionalProperties": False,
            },
            handler=presentation_generate_handler,
            access_level="write",
            mutates_state=True,
            requires_unlocked_workspace=True,
        ))
    return registry
