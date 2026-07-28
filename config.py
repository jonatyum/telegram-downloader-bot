import os
from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    """Lee un entero de entorno; si está vacío o mal formado usa el default."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


BOT_TOKEN: str = os.environ["BOT_TOKEN"]

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
MAX_TELEGRAM_SIZE_BYTES = 50 * 1024 * 1024   # 50 MB — límite del Bot API para enviar como video
# Tope para enviar como documento. En hosts con poca RAM (Render free 512 MB) hay
# que bajarlo, porque enviar bufferiza el archivo entero en memoria (~1.5-2x su tamaño).
MAX_DOCUMENT_SIZE_BYTES = _env_int("MAX_DOCUMENT_SIZE_MB", 2000) * 1024 * 1024

SUPPORTED_DOMAINS = [
    "tiktok.com",
    "vm.tiktok.com",
    "instagram.com",
    "instagr.am",
    "facebook.com",
    "fb.watch",
    "youtu.be",
    "youtube.com",
    "twitter.com",
    "x.com",
    "t.co",
]

MAX_VIDEO_HEIGHT = 1080       # resolución máxima de descarga (1080p)
# Rechaza videos estimados por encima de este tamaño (preflight). En hosts con poca
# RAM conviene bajarlo, ya que enviar un archivo grande lo carga entero en memoria.
MAX_PREFLIGHT_SIZE_BYTES = _env_int("MAX_PREFLIGHT_SIZE_MB", 150) * 1024 * 1024

# Resolución máxima al RE-COMPRIMIR con ffmpeg (libx264). La RAM de la compresión
# escala con la resolución, no con el tamaño del archivo: cap a 720p en hosts con
# poca RAM baja el pico de ~200 MB (1080p) a ~100 MB. Por defecto = calidad de descarga.
MAX_COMPRESS_HEIGHT = _env_int("MAX_COMPRESS_HEIGHT", MAX_VIDEO_HEIGHT)

# Rate limiting: máximo de requests por usuario en una ventana de tiempo
RATE_LIMIT_REQUESTS = 8   # máximo de descargas
RATE_LIMIT_WINDOW = 60    # en segundos (ventana deslizante)

# Descargas simultáneas máximas. Cada una puede estar comprimiendo/enviando a la vez,
# y ambas fases consumen RAM, así que en hosts con poca memoria conviene 1-2.
MAX_CONCURRENT_DOWNLOADS = _env_int("MAX_CONCURRENT_DOWNLOADS", 5)

# Retry a nivel de operación completa para errores transitorios (red/extracción).
# Los retries internos de yt-dlp (retries/fragment_retries) cubren cortes dentro de
# una descarga; esto reintenta el flujo entero cuando extract_info falla por algo pasajero.
MAX_DOWNLOAD_ATTEMPTS = 2     # intentos totales (1 reintento)
RETRY_BACKOFF_SECONDS = 2     # espera entre intentos

ADMIN_CHAT_ID: str | None = os.getenv("ADMIN_CHAT_ID")

# Puerto donde escucha el servidor HTTP. Render inyecta PORT automáticamente;
# HEALTH_PORT se mantiene como fallback para el health server en modo polling.
PORT: int = int(os.getenv("PORT", os.getenv("HEALTH_PORT", "8080")))
HEALTH_PORT: int = PORT

# Conexión a Postgres (Supabase). Usar la connection string del pooler en modo
# transaction (puerto 6543) para que aguante los reinicios del plan free.
DATABASE_URL: str = os.getenv("DATABASE_URL", "")

# URL pública HTTPS para el webhook. En Render se toma de RENDER_EXTERNAL_URL
# (inyectada por la plataforma). Si está vacía, el bot arranca en modo polling.
WEBHOOK_URL: str = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")

# Token secreto opcional que Telegram enviará en cada webhook (defensa extra).
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")
