"""OpenAI-compatible cloud model configuration and transport."""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


CREDENTIAL_TARGET = "SoulDrive.CloudModel"
MAX_MODEL_COUNT = 100


class CloudModelConfigurationError(ValueError):
    pass


class CloudModelRequestError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CloudModelConfig:
    base_url: str
    api_key: str
    model: str
    enabled: bool
    models: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "configured": bool(self.base_url and self.api_key),
            "enabled": self.enabled,
            "base_url": self.base_url,
            "model": self.model,
            "models": list(self.models),
            "api_key_configured": bool(self.api_key),
        }


def normalize_base_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise CloudModelConfigurationError("API 请求地址不能为空")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise CloudModelConfigurationError("API 请求地址必须是 http 或 https 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CloudModelConfigurationError("API 请求地址不能包含账号、密码、查询参数或片段")

    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))


def _candidate_base_urls(value: str) -> list[str]:
    normalized = normalize_base_url(value)
    if not urlsplit(normalized).path:
        return [f"{normalized}/v1", normalized]
    return [normalized]


def load_cloud_model_config() -> CloudModelConfig | None:
    payload = _read_credential()
    if not isinstance(payload, dict):
        return None
    try:
        base_url = normalize_base_url(str(payload.get("base_url") or ""))
    except CloudModelConfigurationError:
        return None
    api_key = str(payload.get("api_key") or "").strip()
    if not api_key:
        return None
    return CloudModelConfig(
        base_url=base_url,
        api_key=api_key,
        model=str(payload.get("model") or "").strip(),
        enabled=bool(payload.get("enabled")),
        models=tuple(_normalize_models(payload.get("models"))),
    )


def save_cloud_model_config(
    base_url: str,
    api_key: str,
    model: str,
    enabled: bool,
    models: list[str] | None = None,
) -> CloudModelConfig:
    normalized_url = normalize_base_url(base_url)
    existing = load_cloud_model_config()
    normalized_key = str(api_key or "").strip() or (
        existing.api_key if existing and existing.base_url == normalized_url else ""
    )
    if not normalized_key:
        raise CloudModelConfigurationError("API Key 不能为空")

    model_name = str(model or "").strip() or (
        existing.model if existing and existing.base_url == normalized_url else ""
    )
    if enabled and not model_name:
        raise CloudModelConfigurationError("启用云端模型前请选择默认模型")

    configured_models = _normalize_models(models)
    if not configured_models and existing and existing.base_url == normalized_url:
        configured_models = list(existing.models)
    payload = {
        "base_url": normalized_url,
        "api_key": normalized_key,
        "model": model_name,
        "enabled": bool(enabled),
        "models": configured_models,
    }
    _write_credential(payload)
    saved = load_cloud_model_config()
    if saved is None:
        raise CloudModelConfigurationError("云端模型配置写入后无法读取")
    return saved


def fetch_cloud_models(base_url: str, api_key: str) -> list[str]:
    normalized_key = str(api_key or "").strip()
    if not normalized_key:
        raise CloudModelConfigurationError("API Key 不能为空")
    last_error = None
    for api_base_url in _candidate_base_urls(base_url):
        request = Request(
            f"{api_base_url}/models",
            headers=_headers(normalized_key),
            method="GET",
        )
        try:
            payload = _read_json(request, normalized_key, timeout=20)
        except CloudModelRequestError as exc:
            if exc.status_code == 404:
                last_error = exc
                continue
            raise
        if isinstance(payload, dict):
            raw_models = payload.get("data") or payload.get("models") or []
        else:
            raw_models = payload
        models = _normalize_models(raw_models)
        if not models:
            raise CloudModelRequestError("云端模型列表为空")
        return models
    if last_error is not None:
        raise last_error
    raise CloudModelRequestError("云端模型列表获取失败")


def saved_or_requested_models(base_url: str, api_key: str) -> list[str]:
    saved = load_cloud_model_config()
    normalized_url = normalize_base_url(base_url or (saved.base_url if saved else ""))
    normalized_key = str(api_key or "").strip()
    if not normalized_key and saved and saved.base_url == normalized_url:
        normalized_key = saved.api_key
    return fetch_cloud_models(normalized_url, normalized_key)


class OpenAICompatibleChatModel:
    """Small adapter implementing the chat interface expected by RAGEngine."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = normalize_base_url(base_url)
        self.base_urls = _candidate_base_urls(base_url)
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        if not self.api_key or not self.model:
            raise CloudModelConfigurationError("云端模型需要 API Key 和默认模型")

    def create_chat_completion(
        self,
        *,
        messages,
        stream=False,
        temperature=0.01,
        top_p=0.85,
        repeat_penalty=1.1,
        max_tokens=900,
        stop=None,
        seed=None,
        response_format=None,
    ):
        del repeat_penalty
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": bool(stream),
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
        }
        if stop:
            payload["stop"] = stop
        if seed is not None:
            payload["seed"] = seed
        if response_format:
            payload["response_format"] = response_format
        if stream:
            return self._stream_response(payload)
        last_error = None
        for api_base_url in self.base_urls:
            request = self._chat_request(api_base_url, payload, stream=False)
            try:
                return _read_json(request, self.api_key, timeout=180)
            except CloudModelRequestError as exc:
                if exc.status_code == 404:
                    last_error = exc
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise CloudModelRequestError("云端模型请求失败")

    def _chat_request(self, api_base_url: str, payload: dict[str, Any], *, stream: bool) -> Request:
        return Request(
            f"{api_base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={**_headers(self.api_key), "Accept": "text/event-stream" if stream else "application/json"},
            method="POST",
        )

    def _stream_response(self, payload):
        last_error = None
        for api_base_url in self.base_urls:
            request = self._chat_request(api_base_url, payload, stream=True)
            try:
                with urlopen(request, timeout=180) as response:
                    for raw_line in response:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line or line.startswith(":") or not line.lower().startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            chunk_payload = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        content = _content_from_choice(chunk_payload)
                        if content:
                            yield {"choices": [{"delta": {"content": content}}]}
                    return
            except HTTPError as exc:
                error = _request_error(exc, self.api_key)
                if error.status_code == 404:
                    last_error = error
                    continue
                raise error from exc
            except (URLError, TimeoutError, OSError) as exc:
                raise CloudModelRequestError("无法连接云端模型接口") from exc
        if last_error is not None:
            raise last_error


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "SoulDrive/1.0",
    }


def _read_json(request: Request, api_key: str, timeout: int) -> Any:
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise _request_error(exc, api_key) from exc
    except json.JSONDecodeError as exc:
        raise CloudModelRequestError("云端模型接口返回了无法解析的响应") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise CloudModelRequestError("无法连接云端模型接口") from exc


def _request_error(exc: HTTPError, api_key: str) -> CloudModelRequestError:
    detail = ""
    try:
        detail = exc.read(512).decode("utf-8", errors="replace")
    except Exception:
        pass
    detail = _redact_error(detail, api_key)
    suffix = f": {detail[:240]}" if detail else ""
    return CloudModelRequestError(f"云端模型接口返回 HTTP {exc.code}{suffix}", status_code=exc.code)


def _redact_error(value: str, api_key: str) -> str:
    redacted = str(value or "").replace(api_key, "[redacted]")
    redacted = re.sub(
        r"(?i)(authorization|api[_-]?key|token|secret|password)\s*[:=]\s*[^,\s}]+",
        r"\1=[redacted]",
        redacted,
    )
    return " ".join(redacted.split())


def _content_from_choice(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
    content = delta.get("content")
    if content is None:
        content = choice.get("text")
    if content is None and isinstance(choice.get("message"), dict):
        content = choice["message"].get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text") or "")
            for item in content
            if isinstance(item, (str, dict))
        )
    return ""


def _normalize_models(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    models: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("id") or item.get("name") or "").strip()
        else:
            name = ""
        if name and name not in models:
            models.append(name[:240])
        if len(models) >= MAX_MODEL_COUNT:
            break
    return models


def _read_credential() -> dict[str, Any] | None:
    try:
        import win32cred
    except ImportError:
        return None
    try:
        item = win32cred.CredRead(CREDENTIAL_TARGET, win32cred.CRED_TYPE_GENERIC)
        blob = item.get("CredentialBlob", "")
        if isinstance(blob, bytes):
            try:
                blob = blob.decode("utf-16-le")
            except UnicodeDecodeError:
                blob = blob.decode("utf-8")
        payload = json.loads(str(blob))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _write_credential(payload: dict[str, Any]) -> None:
    try:
        import win32cred
    except ImportError as exc:
        raise CloudModelConfigurationError("当前 Python 环境缺少 Windows 凭据管理器组件") from exc
    try:
        win32cred.CredWrite(
            {
                "Type": win32cred.CRED_TYPE_GENERIC,
                "TargetName": CREDENTIAL_TARGET,
                "UserName": "SoulDrive Cloud Model",
                "CredentialBlob": json.dumps(payload, ensure_ascii=False),
                "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
            },
            0,
        )
    except Exception as exc:
        raise CloudModelConfigurationError("无法写入 Windows 凭据管理器") from exc
