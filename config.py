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


# Vacío por defecto (no os.environ[...]) para que módulos compartidos con el canal web
# (api.py, que no habla con Telegram) puedan importar config sin necesitar este token.
# bot.py valida que no esté vacío en su propio main(), donde sí es obligatorio.
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# Ruta a un cookies.txt (formato Netscape) con una sesión de YouTube. Vacío = sin cookies.
# YouTube bloquea las IPs de datacenter con "Sign in to confirm you're not a bot"; una
# sesión autenticada es lo único que lo evita de forma fiable. En Render se monta como
# Secret File (/etc/secrets/...), que es de SOLO LECTURA — por eso downloader.py trabaja
# sobre una copia: yt-dlp reescribe el cookiefile al cerrar y fallaría sobre el original.
YOUTUBE_COOKIES_FILE: str = os.getenv("YOUTUBE_COOKIES_FILE", "")

# Clientes de InnerTube que yt-dlp prueba en YouTube, en orden y separados por coma.
# El chequeo antibot no se aplica igual a todos: el cliente `web` (parte del "default")
# es al que primero se lo exigen desde una IP de datacenter, mientras que el de TV y el
# de visores VR a veces siguen respondiendo desde esa misma IP. yt-dlp prueba todos los
# de la lista y junta los formatos de los que contesten, así que sumar clientes solo
# puede ayudar; el costo es una petición más por extracción. Un cliente desconocido se
# ignora con un warning, no rompe. Poner solo "default" deja el comportamiento de fábrica.
YOUTUBE_PLAYER_CLIENTS: tuple[str, ...] = tuple(
    c.strip()
    for c in os.getenv("YOUTUBE_PLAYER_CLIENTS", "default,tv_simply,android_vr").split(",")
    if c.strip()
)

# Proxy usado SOLO para YouTube (extracción y descarga). Vacío = sin proxy.
# El chequeo antibot de YouTube es por reputación de IP: desde una IP de datacenter
# (Render corre sobre Google Cloud, que YouTube reconoce como propia) ninguna librería
# ni cliente lo esquiva de forma fiable, porque el bloqueo ocurre antes de mirar quién
# pregunta. Sin cookies, lo único que lo resuelve es que la petición salga de otra IP.
# Formato de yt-dlp: "http://usuario:clave@host:puerto" (también socks5://).
# No se aplica al resto de plataformas: TikTok, Instagram, Facebook y X funcionan
# directo desde Render, y pasarlas por un proxy de pago sería tirar tráfico y plata.
YOUTUBE_PROXY: str = os.getenv("YOUTUBE_PROXY", "")

# --- YouTube: worker remoto en una conexión residencial ---
# YouTube firma las URLs de los formatos con la IP que las pidió: el parámetro "ip" va
# dentro de "sparams", o sea que está cubierto por la firma. Por eso NO alcanza con
# resolver el link en una máquina de casa y bajar los bytes desde Render — el servidor
# pediría el archivo con otra IP y recibiría un 403. El worker hace la descarga entera
# y devuelve el archivo ya terminado.
# Vacío = sin worker: cada servicio resuelve todo por su cuenta, como hasta ahora.
YOUTUBE_WORKER_URL: str = os.getenv("YOUTUBE_WORKER_URL", "").rstrip("/")

# Secreto compartido, obligatorio en la práctica: el túnel deja el worker expuesto a
# internet y sin esto cualquiera puede usar tu conexión de casa para descargar.
YOUTUBE_WORKER_TOKEN: str = os.getenv("YOUTUBE_WORKER_TOKEN", "")

# Techo para una descarga completa a través del worker: incluye lo que tarda en bajar
# de YouTube más lo que tarda en subirle el archivo a Render por la conexión de casa,
# que suele ser la parte lenta.
YOUTUBE_WORKER_TIMEOUT = _env_int("YOUTUBE_WORKER_TIMEOUT", 300)

# Tras un fallo de conexión, cuánto se deja de intentar. Es lo que hace que apagar la
# máquina no degrade el servicio: sin esto, cada link de YouTube esperaría el timeout
# completo antes de caer al camino local.
YOUTUBE_WORKER_COOLDOWN = _env_int("YOUTUBE_WORKER_COOLDOWN", 300)

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

# URL pública del canal web. El bot la ofrece cuando un video supera el límite de
# 50 MB del Bot API de Telegram: la web no tiene ese tope, así que es la salida
# natural de ese callejón. Vacía = el bot no menciona la web (el aviso de límite
# se queda como estaba), para que un despliegue sin canal web no prometa nada roto.
WEB_URL: str = os.getenv("WEB_URL", "")

# Avisar al admin "Bot iniciado" en cada arranque. En Render free el bot se levanta
# de cero cada vez que despierta del sleep, así que por defecto está apagado para no
# spamear. Ponlo a 1 solo si quieres ver cada arranque.
NOTIFY_ON_START: bool = os.getenv("NOTIFY_ON_START", "").strip().lower() in ("1", "true", "yes", "on")

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
