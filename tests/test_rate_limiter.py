import time
import pytest
from unittest.mock import patch
from rate_limiter import RateLimiter
from config import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW

# Los tests se derivan de la config real en vez de hardcodear el límite: así
# subir/bajar RATE_LIMIT_REQUESTS no deja la suite en rojo por un número obsoleto.
_PAST_WINDOW = RATE_LIMIT_WINDOW + 1


@pytest.fixture
def limiter():
    return RateLimiter()


def _fill(limiter, user_id=1):
    """Consume la cuota completa del usuario."""
    for _ in range(RATE_LIMIT_REQUESTS):
        limiter.is_allowed(user_id)


class TestRateLimiter:
    def test_first_request_allowed(self, limiter):
        assert limiter.is_allowed(user_id=1) is True

    def test_requests_within_limit_allowed(self, limiter):
        for _ in range(RATE_LIMIT_REQUESTS):
            assert limiter.is_allowed(user_id=1) is True

    def test_request_exceeding_limit_blocked(self, limiter):
        _fill(limiter)
        assert limiter.is_allowed(user_id=1) is False

    def test_different_users_independent(self, limiter):
        _fill(limiter, user_id=1)
        # usuario 1 bloqueado, usuario 2 libre
        assert limiter.is_allowed(user_id=1) is False
        assert limiter.is_allowed(user_id=2) is True

    def test_requests_reset_after_window(self, limiter):
        _fill(limiter)
        assert limiter.is_allowed(user_id=1) is False

        # Simula que la ventana expiró
        with patch("rate_limiter.time.time", return_value=time.time() + _PAST_WINDOW):
            assert limiter.is_allowed(user_id=1) is True

    def test_seconds_until_reset_zero_when_not_limited(self, limiter):
        assert limiter.seconds_until_reset(user_id=99) == 0

    def test_seconds_until_reset_positive_when_limited(self, limiter):
        _fill(limiter)
        limiter.is_allowed(user_id=1)  # bloqueado
        assert limiter.seconds_until_reset(user_id=1) > 0

    def test_sliding_window_allows_after_oldest_expires(self, limiter):
        base = time.time()

        # Cuota completa al inicio de la ventana
        with patch("rate_limiter.time.time", return_value=base):
            _fill(limiter)

        # A mitad de ventana sigue bloqueado
        with patch("rate_limiter.time.time", return_value=base + RATE_LIMIT_WINDOW / 2):
            assert limiter.is_allowed(user_id=1) is False

        # Tras la ventana completa se libera
        with patch("rate_limiter.time.time", return_value=base + _PAST_WINDOW):
            assert limiter.is_allowed(user_id=1) is True


class TestRateLimiterCustomLimits:
    """
    Un canal con su propia clave de usuario (p. ej. una IP en el canal web) puede pedir
    límites propios sin heredar los de Telegram — es lo que usa api.py para que
    WEB_RATE_LIMIT_REQUESTS sea independiente de RATE_LIMIT_REQUESTS del bot.
    """

    def test_custom_max_requests_overrides_config_default(self):
        limiter = RateLimiter(max_requests=2)
        assert limiter.is_allowed("1.2.3.4") is True
        assert limiter.is_allowed("1.2.3.4") is True
        assert limiter.is_allowed("1.2.3.4") is False

    def test_default_still_matches_config_when_not_overridden(self):
        limiter = RateLimiter()
        for _ in range(RATE_LIMIT_REQUESTS):
            assert limiter.is_allowed("x") is True
        assert limiter.is_allowed("x") is False

    def test_custom_window_overrides_config_default(self):
        limiter = RateLimiter(max_requests=1, window_seconds=10)
        base = time.time()
        with patch("rate_limiter.time.time", return_value=base):
            assert limiter.is_allowed("k") is True
            assert limiter.is_allowed("k") is False
        with patch("rate_limiter.time.time", return_value=base + 11):
            assert limiter.is_allowed("k") is True

    def test_two_instances_are_independent(self):
        strict = RateLimiter(max_requests=1)
        lenient = RateLimiter(max_requests=100)
        assert strict.is_allowed("k") is True
        assert strict.is_allowed("k") is False
        # El límite estricto no afecta al otro limiter, aunque compartan la clave "k".
        assert lenient.is_allowed("k") is True
