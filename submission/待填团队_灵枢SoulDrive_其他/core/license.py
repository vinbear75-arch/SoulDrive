import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from core.enterprise_policy import EnterprisePolicy, load_policy
from core.paths import app_root


LICENSE_FILENAME = "license.json"
PUBLIC_KEY_ENV = "SOULDRIVE_LICENSE_PUBLIC_KEY"


@dataclass(frozen=True)
class LicenseStatus:
    valid: bool
    level: str
    reason: str
    source: str | None = None
    license_id: str | None = None
    subject: str | None = None
    expires_at: int | None = None
    hardware_hash: str | None = None
    features: list[str] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "level": self.level,
            "reason": self.reason,
            "source": _public_license_source(self.source),
            "license_id": self.license_id,
            "subject": self.subject,
            "expires_at": self.expires_at,
            "hardware_bound": bool(self.hardware_hash),
            "features": self.features,
        }


def hardware_fingerprint(hardware_sn: str | None) -> str | None:
    if not hardware_sn:
        return None
    return hashlib.sha256(hardware_sn.encode("utf-8")).hexdigest()


def license_search_paths(workspace_path: str | None = None) -> list[Path]:
    configured = os.environ.get("SOULDRIVE_LICENSE_PATH")
    paths: list[Path] = []
    if configured:
        paths.append(Path(configured))
    if workspace_path:
        paths.append(Path(workspace_path) / "config" / LICENSE_FILENAME)
    paths.append(app_root() / "config" / LICENSE_FILENAME)
    return paths


def find_license_path(workspace_path: str | None = None) -> Path | None:
    for path in license_search_paths(workspace_path):
        if path.exists():
            return path
    return None


def verify_license_for_workspace(
    workspace_path: str | None,
    hardware_sn: str | None,
    policy: EnterprisePolicy | None = None,
    now: float | None = None,
) -> LicenseStatus:
    effective_policy = policy or load_policy()
    path = find_license_path(workspace_path)
    if path is None:
        return LicenseStatus(False, "NONE", "license file not found")
    return verify_license_file(
        path,
        hardware_sn=hardware_sn,
        require_signature=effective_policy.require_signed_license,
        now=now,
    )


def verify_license_file(
    path: str | os.PathLike[str],
    hardware_sn: str | None = None,
    require_signature: bool = True,
    now: float | None = None,
    public_key_b64: str | None = None,
) -> LicenseStatus:
    source = str(path)
    try:
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        return LicenseStatus(False, "NONE", f"license unreadable: {exc}", source=source)

    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if not isinstance(payload, dict):
        return LicenseStatus(False, "NONE", "license payload missing", source=source)

    signature = envelope.get("signature") if isinstance(envelope, dict) else None
    public_key = public_key_b64 if public_key_b64 is not None else os.environ.get(PUBLIC_KEY_ENV)
    if signature:
        if not public_key:
            return LicenseStatus(False, "NONE", "license public key not configured", source=source)
        if not _verify_signature(payload, str(signature), public_key):
            return LicenseStatus(False, "NONE", "license signature invalid", source=source)
    elif require_signature:
        return LicenseStatus(False, "NONE", "signed license required", source=source)

    expires_at = _optional_int(payload.get("expires_at"))
    current_time = int(time.time() if now is None else now)
    if expires_at is not None and expires_at < current_time:
        return _status_from_payload(payload, False, "license expired", source)

    expected_hardware_hash = payload.get("hardware_hash")
    actual_hardware_hash = hardware_fingerprint(hardware_sn)
    if expected_hardware_hash and expected_hardware_hash != actual_hardware_hash:
        return _status_from_payload(payload, False, "license hardware mismatch", source)

    return _status_from_payload(payload, True, "license verified", source)


def authorization_from_hardware_and_license(
    hardware_level: str,
    hardware_sn: str | None,
    workspace_path: str | None,
    policy: EnterprisePolicy | None = None,
) -> tuple[str, str | None, LicenseStatus]:
    effective_policy = policy or load_policy()
    license_status = verify_license_for_workspace(workspace_path, hardware_sn, effective_policy)
    if license_status.valid:
        return license_status.level, hardware_sn, license_status

    if effective_policy.require_signed_license:
        return "NONE", hardware_sn, license_status

    if hardware_level == "LITE" and not effective_policy.allow_lite_mode:
        blocked = LicenseStatus(False, "NONE", "lite mode disabled by enterprise policy", source=license_status.source)
        return "NONE", hardware_sn, blocked

    return hardware_level, hardware_sn, license_status


def _status_from_payload(payload: dict[str, Any], valid: bool, reason: str, source: str) -> LicenseStatus:
    level = str(payload.get("level") or "NONE").upper()
    if level not in {"NONE", "LITE", "PRO", "ENTERPRISE"}:
        level = "NONE"
    features = payload.get("features")
    return LicenseStatus(
        valid=valid,
        level=level if valid else "NONE",
        reason=reason,
        source=source,
        license_id=str(payload.get("license_id")) if payload.get("license_id") else None,
        subject=str(payload.get("subject")) if payload.get("subject") else None,
        expires_at=_optional_int(payload.get("expires_at")),
        hardware_hash=str(payload.get("hardware_hash")) if payload.get("hardware_hash") else None,
        features=[str(item) for item in features] if isinstance(features, list) else [],
    )


def _verify_signature(payload: dict[str, Any], signature_b64: str, public_key_b64: str) -> bool:
    try:
        public_key_bytes = base64.b64decode(public_key_b64)
        signature = base64.b64decode(signature_b64)
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(public_key_bytes)
        public_key.verify(signature, canonical_payload(payload))
        return True
    except (ValueError, InvalidSignature):
        return False


def canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _public_license_source(source: str | None) -> str | None:
    if not source:
        return None
    return Path(source).name or "license file"
