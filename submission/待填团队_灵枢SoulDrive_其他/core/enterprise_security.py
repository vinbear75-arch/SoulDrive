import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Any

from core.rag_defaults import DEFAULT_RAG_TOP_K
from core.enterprise_policy import load_policy


DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
    "http://tauri.localhost",
)
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
DEFAULT_RATE_LIMIT_PER_MINUTE = 120
SENSITIVE_KEYWORDS = ("token", "secret", "password", "api_key", "hardware_sn", "serial")
WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[A-Z]:\\[^\s\r\n\t\"']+")
POSIX_PATH_PATTERN = re.compile(r"(?<!\w)/(?:[^\s\r\n\t\"']+/)+[^\s\r\n\t\"']+")


def allowed_origins() -> list[str]:
    configured = os.environ.get("SOULDRIVE_ALLOWED_ORIGINS")
    if not configured:
        return list(load_policy().allowed_origins)
    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    return origins or list(load_policy().allowed_origins)


def max_body_bytes() -> int:
    return _positive_int_from_env("SOULDRIVE_MAX_BODY_BYTES", load_policy().max_request_body_bytes)


def rate_limit_per_minute() -> int:
    return _positive_int_from_env("SOULDRIVE_RATE_LIMIT_PER_MINUTE", load_policy().rate_limit_per_minute)


def max_chat_top_k() -> int:
    configured = _positive_int_from_env("SOULDRIVE_MAX_CHAT_TOP_K", load_policy().max_chat_top_k)
    return max(1, min(configured, 20))


def clamp_chat_top_k(value: int | None) -> int:
    try:
        parsed = int(value) if value is not None else DEFAULT_RAG_TOP_K
    except (TypeError, ValueError):
        parsed = DEFAULT_RAG_TOP_K
    return max(1, min(parsed, max_chat_top_k()))


def is_loopback_client(host: str | None) -> bool:
    if not host:
        return True
    normalized = host.strip().lower()
    if normalized in {"localhost", "local", "testclient"}:
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                sanitized[key_text] = _redacted_hash(item)
            else:
                sanitized[key_text] = sanitize_payload(item)
        return sanitized

    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]

    if isinstance(value, tuple):
        return [sanitize_payload(item) for item in value]

    if isinstance(value, str):
        return _redact_local_paths(value)

    return value


@dataclass
class SlidingWindowRateLimiter:
    limit: int
    window_seconds: float = 60.0
    _hits: dict[str, deque[float]] = field(default_factory=dict)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        hits = self._hits.setdefault(key, deque())
        expires_before = now - self.window_seconds

        while hits and hits[0] <= expires_before:
            hits.popleft()

        if len(hits) >= self.limit:
            return False

        hits.append(now)
        return True


def _positive_int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(keyword in normalized for keyword in SENSITIVE_KEYWORDS)


def _redacted_hash(value: Any) -> str | None:
    if value in (None, ""):
        return None
    import hashlib

    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _redact_local_paths(value: str) -> str:
    redacted = WINDOWS_PATH_PATTERN.sub("[local path]", value)
    return POSIX_PATH_PATTERN.sub("[local path]", redacted)
