import pytest

from core import mcp_server
from core.enterprise_security import SlidingWindowRateLimiter


@pytest.fixture(autouse=True)
def isolate_local_api_rate_limiter():
    """Keep API tests independent; production rate limiting remains unchanged."""
    mcp_server.rate_limiter = SlidingWindowRateLimiter(limit=1000)
    yield
