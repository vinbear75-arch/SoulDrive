import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.paths import app_root


POLICY_FILENAME = "enterprise-policy.json"


@dataclass(frozen=True)
class EnterprisePolicy:
    organization: str = "SoulDrive Local"
    allow_lite_mode: bool = True
    require_signed_license: bool = False
    disable_network_update: bool = True
    audit_retention_days: int = 180
    max_chat_top_k: int = 8
    max_request_body_bytes: int = 1024 * 1024
    rate_limit_per_minute: int = 120
    allowed_origins: tuple[str, ...] = (
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "tauri://localhost",
        "http://tauri.localhost",
    )
    model_manifest_required: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def public_dict(self) -> dict[str, Any]:
        return {
            "organization": self.organization,
            "allow_lite_mode": self.allow_lite_mode,
            "require_signed_license": self.require_signed_license,
            "disable_network_update": self.disable_network_update,
            "audit_retention_days": self.audit_retention_days,
            "max_chat_top_k": self.max_chat_top_k,
            "max_request_body_bytes": self.max_request_body_bytes,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "allowed_origins": list(self.allowed_origins),
            "model_manifest_required": self.model_manifest_required,
        }


def policy_path() -> Path:
    configured = os.environ.get("SOULDRIVE_POLICY_PATH")
    if configured:
        return Path(configured)
    return app_root() / "config" / POLICY_FILENAME


def ensure_policy_file(path: Path | None = None) -> Path:
    target = path or policy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(
            json.dumps(default_policy().public_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return target


def load_policy(path: Path | None = None) -> EnterprisePolicy:
    target = path or policy_path()
    if not target.exists():
        return default_policy()

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return default_policy()
    if not isinstance(payload, dict):
        return default_policy()

    return EnterprisePolicy(
        organization=str(payload.get("organization") or "SoulDrive Local")[:120],
        allow_lite_mode=_bool_value(payload.get("allow_lite_mode"), True),
        require_signed_license=_bool_value(payload.get("require_signed_license"), False),
        disable_network_update=_bool_value(payload.get("disable_network_update"), True),
        audit_retention_days=_bounded_int(payload.get("audit_retention_days"), 180, 1, 3650),
        max_chat_top_k=_bounded_int(payload.get("max_chat_top_k"), 8, 1, 20),
        max_request_body_bytes=_bounded_int(payload.get("max_request_body_bytes"), 1024 * 1024, 16 * 1024, 50 * 1024 * 1024),
        rate_limit_per_minute=_bounded_int(payload.get("rate_limit_per_minute"), 120, 1, 10000),
        allowed_origins=tuple(_string_list(payload.get("allowed_origins")) or default_policy().allowed_origins),
        model_manifest_required=_bool_value(payload.get("model_manifest_required"), True),
        raw=payload if isinstance(payload, dict) else {},
    )


def default_policy() -> EnterprisePolicy:
    return EnterprisePolicy()


def production_policy(organization: str = "SoulDrive Enterprise") -> EnterprisePolicy:
    return EnterprisePolicy(
        organization=organization,
        allow_lite_mode=False,
        require_signed_license=True,
        disable_network_update=True,
        audit_retention_days=365,
        max_chat_top_k=8,
        max_request_body_bytes=1024 * 1024,
        rate_limit_per_minute=120,
        model_manifest_required=True,
    )


def validate_policy_for_production(policy: EnterprisePolicy) -> dict[str, Any]:
    issues = []
    if not policy.require_signed_license:
        issues.append("SIGNED_LICENSE_NOT_REQUIRED")
    if policy.allow_lite_mode:
        issues.append("LITE_MODE_ALLOWED")
    if not policy.disable_network_update:
        issues.append("NETWORK_UPDATE_ENABLED")
    if not policy.model_manifest_required:
        issues.append("MODEL_MANIFEST_NOT_REQUIRED")
    if policy.audit_retention_days < 180:
        issues.append("AUDIT_RETENTION_TOO_SHORT")
    if any(origin.strip() == "*" for origin in policy.allowed_origins):
        issues.append("CORS_WILDCARD_ALLOWED")

    return {
        "ready": not issues,
        "issues": issues,
        "policy": policy.public_dict(),
    }


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(parsed, maximum))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
