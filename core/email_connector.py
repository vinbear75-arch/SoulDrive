"""Local SMTP delivery for confirmed SoulDrive email drafts."""

import hashlib
import json
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Any


PROVIDERS = {
    "qq": {"host": "smtp.qq.com", "port": 465},
    "netease163": {"host": "smtp.163.com", "port": 465},
}
TARGET_PREFIX = "SoulDrive.Email.SMTP."


class EmailConfigurationError(ValueError):
    pass


class EmailDeliveryError(RuntimeError):
    pass


def configure_smtp(provider: str, sender: str, authorization_code: str) -> dict[str, Any]:
    provider = str(provider or "").strip().lower()
    sender = str(sender or "").strip()
    authorization_code = str(authorization_code or "").strip()
    if provider not in PROVIDERS:
        raise EmailConfigurationError("unsupported SMTP provider")
    if "@" not in sender or not authorization_code:
        raise EmailConfigurationError("sender and SMTP authorization code are required")

    # Validate the credential before persisting it.  Saving a string to the
    # Windows Credential Manager alone does not prove that QQ/163 will accept
    # it, and would otherwise leave the UI reporting "configured" until the
    # first real send fails.
    candidate = {**PROVIDERS[provider], "provider": provider, "sender": sender, "authorization_code": authorization_code}
    _verify_smtp_login(candidate)
    _write_credential(_target(sender), json.dumps({"provider": provider, "sender": sender, "authorization_code": authorization_code}))
    if _read_config(sender) is None:
        raise EmailConfigurationError("SMTP 凭据写入后无法从 Windows 凭据管理器读取")
    return {"configured": True, "provider": provider, "sender": sender}


def smtp_status(sender: str) -> dict[str, Any]:
    sender = str(sender or "").strip()
    if not sender:
        return {"configured": False, "provider": None, "sender": None, "reason": "sender_required"}
    config = _read_config(sender)
    if config is not None:
        return {"configured": True, "provider": config["provider"], "sender": sender, "reason": "configured"}
    return {
        "configured": False,
        "provider": None,
        "sender": None,
        "reason": "credential_present_but_invalid" if _credential_exists(sender) else "credential_missing",
    }


def send_confirmed_email(plan: dict[str, Any], attachments: list[tuple[str, bytes]]) -> dict[str, Any]:
    sender = str(plan.get("sender") or "").strip()
    recipient = dict(plan.get("recipient") or {})
    recipient_email = str(recipient.get("email") or "").strip()
    if not sender or not recipient_email:
        raise EmailDeliveryError("email draft is missing sender or recipient")
    config = _read_config(sender)
    if config is None:
        raise EmailConfigurationError("SMTP is not configured for this sender")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient_email
    message["Subject"] = str(plan.get("subject") or "")
    message["Message-ID"] = f"<{hashlib.sha256(str(plan.get('idempotency_key') or '').encode('utf-8')).hexdigest()}@souldrive.local>"
    message.set_content(str(plan.get("body") or ""))
    for filename, payload in attachments:
        content_type, _ = mimetypes.guess_type(filename)
        maintype, subtype = (content_type or "application/octet-stream").split("/", 1)
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    try:
        with smtplib.SMTP_SSL(config["host"], config["port"], timeout=25) as client:
            client.login(sender, config["authorization_code"])
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError("SMTP delivery failed") from exc
    return {"provider": config["provider"], "sender": sender, "recipient": recipient_email, "attachment_count": len(attachments)}


def _verify_smtp_login(config: dict[str, Any]) -> None:
    try:
        with smtplib.SMTP_SSL(config["host"], config["port"], timeout=25) as client:
            client.login(config["sender"], config["authorization_code"])
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailConfigurationError("SMTP 登录核验失败，请确认发件邮箱已开启 SMTP 且授权码正确") from exc


def _target(sender: str) -> str:
    return TARGET_PREFIX + hashlib.sha256(sender.casefold().encode("utf-8")).hexdigest()


def _write_credential(target: str, value: str) -> None:
    try:
        import win32cred
    except ImportError as exc:
        raise EmailConfigurationError("当前 Python 环境缺少 Windows 凭据管理器组件，请使用 souldrive 环境启动") from exc
    try:
        win32cred.CredWrite({
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": target,
            "UserName": "SoulDrive SMTP",
            # pywin32 expects a Unicode string here.  Passing encoded bytes raises
            # TypeError before the credential reaches Windows Credential Manager.
            "CredentialBlob": value,
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        }, 0)
    except Exception as exc:
        raise EmailConfigurationError("无法写入 Windows 凭据管理器") from exc


def _credential_exists(sender: str) -> bool:
    try:
        import win32cred
        win32cred.CredRead(_target(sender), win32cred.CRED_TYPE_GENERIC)
        return True
    except Exception:
        return False


def _read_config(sender: str) -> dict[str, Any] | None:
    try:
        import win32cred
        item = win32cred.CredRead(_target(sender), win32cred.CRED_TYPE_GENERIC)
        blob = item["CredentialBlob"]
        if isinstance(blob, bytes):
            blob = blob.decode("utf-16-le")
        payload = json.loads(str(blob))
    except Exception:
        return None
    provider = payload.get("provider")
    if (
        provider not in PROVIDERS
        or str(payload.get("sender") or "").casefold() != sender.casefold()
        or not payload.get("authorization_code")
    ):
        return None
    return {**PROVIDERS[provider], **payload}
