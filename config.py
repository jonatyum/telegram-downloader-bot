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


def _env_float(name: str, default: float) -> float:
    """Igual que _env_int, para los valores que no son enteros (dBFS, LUFS)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
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

# Hilos que se le permiten a ffmpeg. NO es un dial de velocidad, es uno de memoria: un
# contenedor ve los cores de la máquina anfitriona, no su cuota de CPU, así que libx264
# abre un hilo por core y reserva un contexto de codificación por hilo. Medido sobre el
# mismo recode a 720x1280: 456 MB con los hilos por defecto, 319 MB con 2 y 296 MB con 1.
# Sumados los 65 MB del proceso del bot, la primera cifra no cabe en un host de 512 MB —
# y con 0,1 vCPU de cuota esos hilos extra tampoco aportan velocidad, solo overhead.
# 0 = dejar decidir a ffmpeg (solo tiene sentido en una máquina con CPU de verdad).
FFMPEG_THREADS = _env_int("FFMPEG_THREADS", 2)

# Duración máxima de video (segundos) que este host acepta RECODIFICAR. Por encima, se
# entrega el formato original sin convertir en vez de intentarlo.
#
# No es una preferencia de calidad, es un límite de plataforma. Medido en producción
# (Render free, 0,1 vCPU): un Reel en VP9 no terminó de convertirse en 300 s, o sea que
# se gastaron cinco minutos para acabar sin nada — y después el fallback volvía a
# descargar desde cero. Fallar rápido vale infinitamente más que intentarlo: la conversión
# o cabe con holgura o no se empieza.
# 0 = no recodificar nunca (ningún video dura 0 s o menos). El default es deliberadamente
# alto: en una máquina con CPU de verdad convertir es cuestión de segundos.
TRANSCODE_MAX_DURATION = _env_int("TRANSCODE_MAX_DURATION", 3600)

# --- Modo "máxima calidad" (/settings del bot) ---
# Tope de resolución del modo. También hace de centinela: es el valor que se guarda en
# users.max_resolution para distinguirlo de una resolución normal (el bot solo ofrece
# hasta 1080p), así que no hizo falta una columna nueva.
BEST_QUALITY_HEIGHT = 2160
# Cap de resolución al recodificar en ese modo. Va aparte de MAX_COMPRESS_HEIGHT porque
# los dos casos son distintos: el recode normal es un accidente (bajó algo raro) y
# conviene capado bajo, mientras que aquí el usuario pidió explícitamente calidad y
# capar a 720p devolvería algo PEOR que el H.264 nativo que se baja sin el modo.
# El costo es real: un libx264 a 1080p pica en ~700 MB de RAM y en un host de 512 MB
# puede morir por OOM — por eso el modo es opt-in y esto es un dial aparte.
MAX_QUALITY_COMPRESS_HEIGHT = _env_int("MAX_QUALITY_COMPRESS_HEIGHT", MAX_VIDEO_HEIGHT)

# --- Normalización de audio ---
# TikTok no deja el volumen "horneado" en el archivo: la app sube la ganancia al
# reproducir usando la loudness que manda su propia API, así que el MP4 que se descarga
# suena mucho más bajo que el mismo video dentro de TikTok (medido con ffmpeg sobre un
# TikTok cualquiera: -27.3 LUFS integrados, contra los -14 LUFS que es el estándar de
# reproducción). Lo mismo, en menor grado, en el resto de plataformas.
# El coste es bajo: se mide el audio y, solo si hace falta, se recodifica SOLO la pista
# de audio copiando el video (~2 s para un video de 37 s), así que no toca el pico de RAM
# que impone libx264 — que es el dial delicado en Render free.
NORMALIZE_AUDIO: bool = os.getenv("NORMALIZE_AUDIO", "1").strip().lower() in ("1", "true", "yes", "on")
# Objetivo de loudness integrada. -14 LUFS es lo que usan YouTube/Spotify/TikTok al
# reproducir; subir más solo comprime el rango dinámico sin sonar mejor.
AUDIO_TARGET_LUFS = _env_float("AUDIO_TARGET_LUFS", -14.0)
# Por debajo de esta ganancia no se toca nada: recodificar el audio para ganar 1 dB no
# se nota y sí cuesta una pasada de ffmpeg más una generación de pérdida.
AUDIO_MIN_GAIN_LU = _env_float("AUDIO_MIN_GAIN_LU", 2.0)

# Rate limiting: máximo de requests por usuario en una ventana de tiempo
RATE_LIMIT_REQUESTS = 8   # máximo de descargas
RATE_LIMIT_WINDOW = 60    # en segundos (ventana deslizante)

# Descargas simultáneas máximas. Cada una puede estar comprimiendo/enviando a la vez,
# y ambas fases consumen RAM, así que en hosts con poca memoria conviene 1-2.
MAX_CONCURRENT_DOWNLOADS = _env_int("MAX_CONCURRENT_DOWNLOADS", 5)

# Techo de hilos para el trabajo síncrono (yt-dlp y ffmpeg corren en un executor).
# Sin fijarlo, asyncio usa el executor por defecto, que crece hasta min(32, cpu+4) hilos:
# el semáforo de descargas no lo limita porque el preflight de cada link corre FUERA de
# él, así que N usuarios simultáneos eran N extracciones de yt-dlp a la vez, cada una con
# su memoria. Con un techo, el consumo es predecible y lo de más espera en la cola.
EXECUTOR_MAX_WORKERS = _env_int("EXECUTOR_MAX_WORKERS", max(4, MAX_CONCURRENT_DOWNLOADS * 2))

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
