import hmac
import os


TOKEN_HEADER = "X-SoulDrive-Token"


def configured_api_token():
    return os.environ.get("SOULDRIVE_API_TOKEN")


def is_authorized_token(token: str | None, configured_token: str | None = None):
    expected = configured_token if configured_token is not None else configured_api_token()
    if not expected:
        return os.environ.get("SOULDRIVE_ALLOW_UNAUTHENTICATED_API") == "1"
    return bool(token) and hmac.compare_digest(str(token), str(expected))
