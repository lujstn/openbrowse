import pytest

from app.auth_throttle import throttle


# @nonobvious(forced-by): the auth throttle is process-global, so failed-auth
# tests in one module would lock out unrelated tests without this reset.
@pytest.fixture(autouse=True)
def _reset_auth_throttle():
    throttle.reset()
    yield
    throttle.reset()
