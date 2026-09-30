import time
from collections import defaultdict, deque
from config import RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW


class RateLimiter:
    """
    Ventana deslizante en memoria. Por defecto usa los límites de Telegram
    (RATE_LIMIT_REQUESTS/RATE_LIMIT_WINDOW de config.py); un canal con su propia clave
    de usuario (p. ej. una IP en el canal web) puede pasar los suyos sin tocar esta clase.

    Las claves no se quedan para siempre, y eso importa más de lo que parece: la clave
    del canal web es la IP del cliente, o sea que la decide cualquiera desde internet.
    Medido antes de arreglarlo: 200.000 IPs distintas dejaban 200.000 claves y +201 MB
    de RSS en un servicio que tiene 512 MB en total.

    La purga es activa (un barrido por ventana), no solo perezosa al tocar cada clave:
    a una IP que no vuelve nunca nadie le vuelve a mirar la ventana, así que limpiar
    únicamente lo que se toca deja fuera justo el caso que hace crecer el dict.
    """

    def __init__(self, max_requests: int | None = None, window_seconds: int | None = None):
        self._timestamps: dict = defaultdict(deque)
        self._max_requests = RATE_LIMIT_REQUESTS if max_requests is None else max_requests
        self._window = RATE_LIMIT_WINDOW if window_seconds is None else window_seconds
        self._last_sweep = time.time()

    def _prune(self, key, now: float) -> deque:
        """Descarta los timestamps de esta clave que ya salieron de la ventana."""
        dq = self._timestamps.get(key)
        if dq is None:
            return deque()
        while dq and dq[0] < now - self._window:
            dq.popleft()
        return dq

    def _sweep(self, now: float) -> None:
        """
        Borra las claves cuya ventana entera ha vencido. Se ejecuta como mucho una vez
        por ventana: es O(claves), y en régimen normal esas claves son un puñado. El
        caso que justifica el recorrido es el contrario — miles de IPs de una sola
        petición que no vuelven — y ahí es precisamente donde hay que liberar.
        """
        if now - self._last_sweep < self._window:
            return
        self._last_sweep = now
        cutoff = now - self._window
        vencidas = [k for k, dq in self._timestamps.items() if not dq or dq[-1] < cutoff]
        for k in vencidas:
            del self._timestamps[k]

    def is_allowed(self, user_id) -> bool:
        now = time.time()
        self._sweep(now)
        dq = self._prune(user_id, now)

        if len(dq) >= self._max_requests:
            return False

        # El defaultdict crea la deque si es la primera petición de esta clave.
        self._timestamps[user_id].append(now)
        return True

    def seconds_until_reset(self, user_id) -> int:
        # .get() y no [user_id]: con un defaultdict, consultar el reset de una clave
        # desconocida la creaba. Un endpoint público bastaba para ir llenando el dict.
        dq = self._timestamps.get(user_id)
        if not dq:
            return 0
        return max(0, int(dq[0] + self._window - time.time()) + 1)

    def active_keys(self) -> int:
        """Claves con ventana viva. Para poder observar el tamaño real desde fuera."""
        return len(self._timestamps)


rate_limiter = RateLimiter()
