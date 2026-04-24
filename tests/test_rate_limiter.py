import time
import pytest
from unittest.mock import patch
from rate_limiter import RateLimiter


@pytest.fixture
def limiter():
    return RateLimiter()


class TestRateLimiter:
    def test_first_request_allowed(self, limiter):
        assert limiter.is_allowed(user_id=1) is True

    def test_requests_within_limit_allowed(self, limiter):
        for _ in range(5):
            assert limiter.is_allowed(user_id=1) is True

    def test_request_exceeding_limit_blocked(self, limiter):
        for _ in range(5):
            limiter.is_allowed(user_id=1)
        assert limiter.is_allowed(user_id=1) is False

    def test_different_users_independent(self, limiter):
        for _ in range(5):
            limiter.is_allowed(user_id=1)
        # usuario 1 bloqueado, usuario 2 libre
        assert limiter.is_allowed(user_id=1) is False
        assert limiter.is_allowed(user_id=2) is True

    def test_requests_reset_after_window(self, limiter):
        for _ in range(5):
            limiter.is_allowed(user_id=1)
        assert limiter.is_allowed(user_id=1) is False

        # Simula que la ventana expiró
        with patch("rate_limiter.time.time", return_value=time.time() + 61):
            assert limiter.is_allowed(user_id=1) is True

    def test_seconds_until_reset_zero_when_not_limited(self, limiter):
        assert limiter.seconds_until_reset(user_id=99) == 0

    def test_seconds_until_reset_positive_when_limited(self, limiter):
        for _ in range(5):
            limiter.is_allowed(user_id=1)
        limiter.is_allowed(user_id=1)  # bloqueado
        assert limiter.seconds_until_reset(user_id=1) > 0

    def test_sliding_window_allows_after_oldest_expires(self, limiter):
        base = time.time()

        # 5 requests al inicio de la ventana
        with patch("rate_limiter.time.time", return_value=base):
            for _ in range(5):
                limiter.is_allowed(user_id=1)

        # A mitad de ventana sigue bloqueado
        with patch("rate_limiter.time.time", return_value=base + 30):
            assert limiter.is_allowed(user_id=1) is False

        # Tras la ventana completa se libera
        with patch("rate_limiter.time.time", return_value=base + 61):
            assert limiter.is_allowed(user_id=1) is True
