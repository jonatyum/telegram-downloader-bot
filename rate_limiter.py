import time
from collections import defaultdict, deque
from config import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW


class RateLimiter:
    """
    Ventana deslizante en memoria. Por defecto usa los límites de Telegram
    (RATE_LIMIT_REQUESTS/RATE_LIMIT_WINDOW de config.py); un canal con su propia clave
    de usuario (p. ej. una IP en el canal web) puede pasar los suyos sin tocar esta clase.
    """

    def __init__(self, max_requests: int | None = None, window_seconds: int | None = None):
        self._timestamps: dict = defaultdict(deque)
        self._max_requests = RATE_LIMIT_REQUESTS if max_requests is None else max_requests
        self._window = RATE_LIMIT_WINDOW if window_seconds is None else window_seconds

    def is_allowed(self, user_id) -> bool:
        now = time.time()
        dq = self._timestamps[user_id]

        # Descarta timestamps fuera de la ventana deslizante
        while dq and dq[0] < now - self._window:
            dq.popleft()

        if len(dq) >= self._max_requests:
            return False

        dq.append(now)
        return True

    def seconds_until_reset(self, user_id) -> int:
        dq = self._timestamps[user_id]
        if not dq:
            return 0
        return max(0, int(dq[0] + self._window - time.time()) + 1)


rate_limiter = RateLimiter()
