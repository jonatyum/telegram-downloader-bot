import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]

DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
MAX_TELEGRAM_SIZE_BYTES = 50 * 1024 * 1024   # 50 MB — límite para enviar como video
MAX_DOCUMENT_SIZE_BYTES = 2000 * 1024 * 1024  # 2 GB — límite absoluto de Telegram

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
MAX_PREFLIGHT_SIZE_BYTES = 150 * 1024 * 1024  # rechazar videos estimados > 150 MB

# Rate limiting: máximo de requests por usuario en una ventana de tiempo
RATE_LIMIT_REQUESTS = 8   # máximo de descargas
RATE_LIMIT_WINDOW = 60    # en segundos (ventana deslizante)

MAX_CONCURRENT_DOWNLOADS = 5  # descargas simultáneas máximas

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
