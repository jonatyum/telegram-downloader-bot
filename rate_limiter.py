import time
from collections import defaultdict, deque
from config import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW


class RateLimiter:
    def __init__(self):
        self._timestamps: dict[int, deque] = defaultdict(deque)

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        dq = self._timestamps[user_id]

        # Descarta timestamps fuera de la ventana deslizante
        while dq and dq[0] < now - RATE_LIMIT_WINDOW:
            dq.popleft()

        if len(dq) >= RATE_LIMIT_REQUESTS:
            return False

        dq.append(now)
        return True

    def seconds_until_reset(self, user_id: int) -> int:
        dq = self._timestamps[user_id]
        if not dq:
            return 0
        return max(0, int(dq[0] + RATE_LIMIT_WINDOW - time.time()) + 1)


rate_limiter = RateLimiter()
