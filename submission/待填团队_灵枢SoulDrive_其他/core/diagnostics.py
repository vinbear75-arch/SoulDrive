import os
from pathlib import Path
from typing import Any

from core.audit_log import AuditLogger
from core.embedding_models import (
    preferred_embedding_model_path,
    required_embedding_files,
)
from core.enterprise_policy import ensure_policy_file, load_policy, policy_path
from core.enterprise_security import max_body_bytes
from core.license import verify_license_for_workspace
from core.model_runtime import model_runtime_diagnostics
from core.paths import app_root, model_search_dirs
from core.runtime_state import get_runtime_state
from core.workspace import SoulDriveWorkspace, resolve_workspace


def product_diagnostics() -> dict[str, Any]:
    ensure_policy_file()
    policy = load_policy()
    local_pc_mode = os.environ.get("SOULDRIVE_PC_ONLY", "0") == "1" or os.environ.get("SOULDRIVE_WORKSPACE_MODE") == "local"
    if local_pc_mode:
        from dataclasses import replace

        policy = replace(policy, allow_lite_mode=True, require_signed_license=False)
    state = get_runtime_state()
    workspace = _diagnostic_workspace(state)
    workspace_path = workspace.root_path if workspace is not None else None
    if workspace is None:
        workspace_report = {
            "ready": False,
            "reason": "waiting for removable SoulDrive workspace",
        }
        audit_report = {
            "ready": False,
            "reason": "waiting for removable SoulDrive workspace",
        }
    else:
        workspace_report = workspace.diagnose()
        audit_report = AuditLogger.for_workspace(workspace).verify_chain(limit=1000)
    model_report = model_diagnostics(workspace_path)
    license_report = verify_license_for_workspace(
        workspace_path,
        state.get("hardware_sn"),
        policy=policy,
    ).public_dict()
    runtime_unlocked = not state.get("locked") and bool(state.get("workspace_path"))

    checks = {
        "policy": policy_path().exists(),
        "models": model_report["ready"] if policy.model_manifest_required else True,
        "workspace": workspace_report["ready"],
        "audit_chain": audit_report["ready"],
        "runtime_state": True,
        "runtime_unlocked": runtime_unlocked,
        "license": license_report["valid"] or not policy.require_signed_license,
    }
    ready = all(checks.values())

    public_policy = policy.public_dict()
    public_policy["max_request_body_bytes"] = max_body_bytes()

    return {
        "ready": ready,
        "app_root": _redact_path(app_root()),
        "checks": checks,
        "policy": public_policy,
        "models": model_report,
        "workspace": workspace_report,
        "audit": audit_report,
        "runtime": {
            "locked": state.get("locked"),
            "auth_level": state.get("auth_level"),
            "reason": state.get("reason"),
            "workspace_mounted": bool(state.get("workspace_path")),
            "local_pc_mode": local_pc_mode,
        },
        "license": license_report,
    }


def model_diagnostics(workspace_path: str | None = None) -> dict[str, Any]:
    missing = []
    resolved = []
    dirs = model_search_dirs(workspace_path)
    if str(os.environ.get("SOULDRIVE_EMBEDDING_PROVIDER") or "").strip().lower() != "ollama":
        embedding_path = Path(preferred_embedding_model_path(workspace_path))
        for filename in required_embedding_files(embedding_path.name):
            candidate = embedding_path / filename
            display_path = f"{embedding_path.name}/{filename}"
            if not candidate.exists():
                missing.append(display_path)
            else:
                resolved.append({
                    "path": display_path,
                    "size": candidate.stat().st_size,
                })
    runtime_report = model_runtime_diagnostics(workspace_path)
    return {
        "ready": not missing and runtime_report["ready"],
        "missing": missing,
        "resolved": resolved,
        "search_roots": [_redact_path(path) for path in dirs],
        "runtime": runtime_report,
    }


def _diagnostic_workspace(state: dict[str, Any]) -> SoulDriveWorkspace | None:
    if state.get("workspace_path"):
        return SoulDriveWorkspace(state["workspace_path"]).ensure()
    if state.get("active_drive"):
        return resolve_workspace(state.get("active_drive"))
    return None


def _redact_path(path: Path) -> str:
    return path.name if path.name else str(path)
