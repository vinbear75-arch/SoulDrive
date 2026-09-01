import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.email_connector import EmailConfigurationError, _read_config, _write_credential, send_confirmed_email, smtp_status


def test_smtp_status_is_not_configured_without_a_windows_credential():
    with patch("core.email_connector._credential_exists", return_value=False):
        assert smtp_status("sender@example.test") == {
            "configured": False,
            "provider": None,
            "sender": None,
            "reason": "credential_missing",
        }


def test_confirmed_email_uses_ssl_smtp_and_never_exposes_the_authorization_code():
    client = MagicMock()
    with patch("core.email_connector._read_config", return_value={
        "provider": "qq", "host": "smtp.qq.com", "port": 465,
        "sender": "sender@example.test", "authorization_code": "private-code",
    }), patch("core.email_connector.smtplib.SMTP_SSL") as smtp:
        smtp.return_value.__enter__.return_value = client
        result = send_confirmed_email({
            "sender": "sender@example.test", "recipient": {"email": "recipient@example.test"},
            "subject": "测试邮件", "body": "正文", "idempotency_key": "test-key",
        }, [("notes.txt", b"attachment")])

    assert result["provider"] == "qq"
    smtp.assert_called_once_with("smtp.qq.com", 465, timeout=25)
    client.login.assert_called_once_with("sender@example.test", "private-code")
    assert client.send_message.call_count == 1
    assert "private-code" not in str(result)


def test_confirmed_email_requires_configured_sender():
    with patch("core.email_connector._read_config", return_value=None):
        with pytest.raises(EmailConfigurationError):
            send_confirmed_email({"sender": "sender@example.test", "recipient": {"email": "recipient@example.test"}}, [])


def test_configure_smtp_verifies_that_the_credential_can_be_read_back():
    from core.email_connector import configure_smtp

    with patch("core.email_connector._verify_smtp_login"), patch("core.email_connector._write_credential") as write, patch(
        "core.email_connector._read_config", return_value=None
    ):
        with pytest.raises(EmailConfigurationError, match="无法从 Windows 凭据管理器读取"):
            configure_smtp("qq", "sender@example.test", "private-code")
    write.assert_called_once()


def test_configure_smtp_rejects_invalid_login_before_writing_credential():
    from core.email_connector import configure_smtp

    with patch("core.email_connector._verify_smtp_login", side_effect=EmailConfigurationError("login failed")), patch(
        "core.email_connector._write_credential"
    ) as write:
        with pytest.raises(EmailConfigurationError, match="login failed"):
            configure_smtp("qq", "sender@example.test", "private-code")
    write.assert_not_called()


def test_smtp_status_distinguishes_invalid_stored_credential():
    with patch("core.email_connector._read_config", return_value=None), patch(
        "core.email_connector._credential_exists", return_value=True
    ):
        assert smtp_status("sender@example.test")["reason"] == "credential_present_but_invalid"


def test_windows_credential_round_trip_uses_text_blob_for_pywin32():
    captured = {}

    def cred_write(payload, _flags):
        captured.update(payload)

    fake_win32cred = SimpleNamespace(
        CRED_TYPE_GENERIC=1,
        CRED_PERSIST_LOCAL_MACHINE=2,
        CredWrite=cred_write,
        CredRead=lambda _target, _kind: {"CredentialBlob": '{"provider":"qq","sender":"sender@example.test","authorization_code":"private-code"}'},
    )

    with patch.dict(sys.modules, {"win32cred": fake_win32cred}):
        _write_credential("target", '{"provider":"qq"}')
        config = _read_config("sender@example.test")

    assert isinstance(captured["CredentialBlob"], str)
    assert config["provider"] == "qq"


def test_windows_credential_sender_match_is_case_insensitive():
    fake_win32cred = SimpleNamespace(
        CRED_TYPE_GENERIC=1,
        CredRead=lambda _target, _kind: {
            "CredentialBlob": '{"provider":"qq","sender":"Sender@Example.Test","authorization_code":"private-code"}'
        },
    )

    with patch.dict(sys.modules, {"win32cred": fake_win32cred}):
        config = _read_config("sender@example.test")

    assert config is not None
    assert config["sender"] == "Sender@Example.Test"
