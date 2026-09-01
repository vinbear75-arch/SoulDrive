import os


DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000


def api_host():
    return os.environ.get("SOULDRIVE_API_HOST", DEFAULT_API_HOST)


def api_port():
    raw_port = os.environ.get("SOULDRIVE_API_PORT")
    if not raw_port:
        return DEFAULT_API_PORT
    try:
        port = int(raw_port)
    except ValueError:
        return DEFAULT_API_PORT
    return port if 1 <= port <= 65535 else DEFAULT_API_PORT


def api_base_url():
    return f"http://{api_host()}:{api_port()}"
