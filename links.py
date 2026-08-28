"""
Detección de links soportados. Vive fuera de bot.py porque api.py también la necesita
(para validar la URL antes de encolar un job) y no tiene por qué importar telegram para
eso — el mismo motivo por el que downloader.py y pipeline.py no importan telegram.
"""
from urllib.parse import urlparse

from config import SUPPORTED_DOMAINS

_YOUTUBE_DOMAINS = {"youtu.be", "youtube.com", "music.youtube.com"}


def is_supported_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.netloc.lower().removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)
    except Exception:
        return False


def is_youtube_url(text: str) -> bool:
    try:
        host = urlparse(text).netloc.lower().removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in _YOUTUBE_DOMAINS)
    except Exception:
        return False


def extract_urls(text: str) -> list[str]:
    """Devuelve todas las URLs soportadas del texto, sin duplicados y en orden."""
    urls: list[str] = []
    seen: set[str] = set()
    for token in text.split():
        if token not in seen and is_supported_url(token):
            seen.add(token)
            urls.append(token)
    return urls
