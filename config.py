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

# Rate limiting: máximo de requests por usuario en una ventana de tiempo
RATE_LIMIT_REQUESTS = 5   # máximo de descargas
RATE_LIMIT_WINDOW = 60    # en segundos (ventana deslizante)

DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")
