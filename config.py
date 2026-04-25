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

MAX_VIDEO_HEIGHT = 720        # resolución máxima de descarga (720p)
MAX_PREFLIGHT_SIZE_BYTES = 100 * 1024 * 1024  # rechazar videos estimados > 100 MB

# Rate limiting: máximo de requests por usuario en una ventana de tiempo
RATE_LIMIT_REQUESTS = 5   # máximo de descargas
RATE_LIMIT_WINDOW = 60    # en segundos (ventana deslizante)

MAX_CONCURRENT_DOWNLOADS = 3  # descargas simultáneas máximas

DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")
