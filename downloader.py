import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from typing import TypeVar
import yt_dlp
from config import (
    AUDIO_MIN_GAIN_LU,
    AUDIO_TARGET_LUFS,
    DOWNLOAD_DIR,
    FFMPEG_THREADS,
    MAX_DOCUMENT_SIZE_BYTES,
    MAX_VIDEO_HEIGHT,
    MAX_COMPRESS_HEIGHT,
    MAX_QUALITY_COMPRESS_HEIGHT,
    MAX_PREFLIGHT_SIZE_BYTES,
    TRANSCODE_MAX_DURATION,
    MAX_DOWNLOAD_ATTEMPTS,
    NORMALIZE_AUDIO,
    RETRY_BACKOFF_SECONDS,
    YOUTUBE_COOKIES_FILE,
    YOUTUBE_PLAYER_CLIENTS,
    YOUTUBE_PROXY,
    YOUTUBE_WORKER_COOLDOWN,
    YOUTUBE_WORKER_TIMEOUT,
    YOUTUBE_WORKER_TOKEN,
    YOUTUBE_WORKER_URL,
)
from links import is_youtube_url

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Marcadores de errores deterministas: reintentar no cambia el resultado, así que
# se propagan de inmediato en vez de gastar otro intento y la slot del semáforo.
_PERMANENT_ERROR_MARKERS = (
    "private",
    "video unavailable",
    "content is unavailable",
    "has been removed",
    "was deleted",
    "does not exist",
    "no longer available",
    "requested format is not available",
    "larger than max-filesize",
    "exceeds the maximum",
    "age-restricted",
    "sign in to confirm your age",
    # Chequeo antibot de YouTube contra IPs de datacenter. Sin apóstrofo en el marcador:
    # el mensaje viene de YouTube y usa el tipográfico ("you’re"), no el ASCII. Reintentar
    # no lo cambia — la IP sigue siendo la misma — y gastaría la slot del semáforo.
    "not a bot",
    "members-only",
    "account has been terminated",
    "not available in your country",
    "geo restriction",
    "unsupported url",
)


def _is_transient_error(err: Exception) -> bool:
    """Un DownloadError se considera transitorio salvo que coincida con un fallo permanente conocido."""
    msg = str(err).lower()
    return not any(marker in msg for marker in _PERMANENT_ERROR_MARKERS)


def _run_with_retry(operation: Callable[[], _T]) -> _T:
    """Ejecuta una descarga reintentando solo ante DownloadError transitorios."""
    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        try:
            return operation()
        except yt_dlp.DownloadError as e:
            if attempt >= MAX_DOWNLOAD_ATTEMPTS or not _is_transient_error(e):
                raise
            logger.warning(
                "Download attempt %d/%d failed (transient): %s. Retrying in %ds...",
                attempt, MAX_DOWNLOAD_ATTEMPTS, e, RETRY_BACKOFF_SECONDS,
            )
            time.sleep(RETRY_BACKOFF_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover


def purge_temp_dir(path: str | None = None) -> int:
    """
    Borra lo que haya quedado en DOWNLOAD_DIR (o en el directorio dado) y devuelve
    cuántos archivos se fueron. Se llama SOLO al arrancar cada servicio.

    Nadie limpiaba esto, y el `finally` de pipeline.py no alcanza: cuando el proceso
    muere a mitad de una descarga —OOM, reinicio de la plataforma, deploy— el archivo
    a medias se queda, igual que los ".part" de un merge de yt-dlp interrumpido. En un
    disco efímero eso no se nota hasta que se llena y empiezan a fallar descargas que
    no tienen nada que ver.

    Los archivos ocultos se respetan: ahí vive la copia de trabajo del cookiefile.
    Y los subdirectorios no se tocan, solo se avisan: en este directorio no debería
    haber ninguno, así que si aparece uno es mejor mirarlo que borrarlo a ciegas.
    """
    target = path or DOWNLOAD_DIR
    if not os.path.isdir(target):
        return 0
    borrados = 0
    for name in os.listdir(target):
        if name.startswith("."):
            continue
        full = os.path.join(target, name)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                logger.warning("purge_temp_dir: %s es un directorio, se deja sin tocar", full)
                continue
            os.remove(full)
            borrados += 1
        except OSError:
            logger.warning("purge_temp_dir: no pude borrar %s", full)
    if borrados:
        logger.info("purge_temp_dir: %d archivo(s) huérfano(s) borrado(s) de %s", borrados, target)
    return borrados


def _make_output_path() -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    return os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.%(ext)s")


# Un post (carrusel) tiene varios items; cada uno necesita un nombre único.
def _make_carousel_template() -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    return os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.%(playlist_index)s.%(ext)s")


# Extensiones que Telegram trata como foto (no video).
_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "heic", "gif"}


def _entry_filepath(entry: dict) -> str | None:
    """Ruta final del archivo descargado para un entry de yt-dlp (tras merge/postproceso)."""
    downloads = entry.get("requested_downloads") or []
    if downloads and downloads[0].get("filepath"):
        return downloads[0]["filepath"]
    return entry.get("filepath")


# User-Agent usado en todas las peticiones a las plataformas.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# Copia de trabajo del cookies.txt. yt-dlp reescribe el cookiefile al cerrar
# (YoutubeDL.close -> cookiejar.save()) para persistir la sesión refrescada, y en Render
# los Secret Files se montan de SOLO LECTURA: apuntarlo al secreto haría fallar cada
# descarga. Se copia una vez a un sitio escribible y yt-dlp trabaja sobre la copia.
_cookies_lock = threading.Lock()
_cookies_workfile: str | None = None
_cookies_prepared = False


def check_youtube_config() -> list[str]:
    """
    Revisa la configuración de YouTube al arrancar y devuelve los problemas encontrados
    (además de loguearlos). Nada de esto rompe el arranque: son avisos.

    Existe porque todos estos fallos son SILENCIOSOS y se manifiestan igual —el chequeo
    antibot de YouTube— mucho después, en la primera descarga de alguien. Un despliegue
    mal configurado parecía sano hasta ese momento, y el error que salía apuntaba a
    YouTube en vez de a la variable que faltaba.
    """
    problemas: list[str] = []
    # "Usables", no "configuradas": unas cookies que apuntan a una ruta inexistente
    # dejan a YouTube igual de desprotegido que no tener ninguna, así que no deben
    # contar como cobertura en la última comprobación.
    cookies_ok = bool(YOUTUBE_COOKIES_FILE) and os.path.exists(YOUTUBE_COOKIES_FILE)

    if YOUTUBE_COOKIES_FILE and not cookies_ok:
        problemas.append(
            "YOUTUBE_COOKIES_FILE apunta a una ruta que no existe: se trabajará sin "
            "cookies. Si no piensas usarlas, deja la variable vacía y este aviso se va."
        )

    if YOUTUBE_WORKER_URL and not YOUTUBE_WORKER_TOKEN:
        # El worker responde 401 a todo, así que cada link de YouTube pagaría el intento
        # y acabaría en local igual. Es el fallo más difícil de ver: el worker está
        # levantado y contesta, solo que rechaza.
        problemas.append(
            "YOUTUBE_WORKER_URL está configurada pero YOUTUBE_WORKER_TOKEN está vacía: "
            "el worker rechazará todas las peticiones con 401 y YouTube caerá siempre "
            "al camino local."
        )

    if not YOUTUBE_WORKER_URL and not cookies_ok and not YOUTUBE_PROXY:
        problemas.append(
            "YouTube sin worker, sin cookies y sin proxy: desde una IP de datacenter el "
            "chequeo antibot lo bloquea casi siempre. El resto de plataformas no se ve "
            "afectado."
        )

    for aviso in problemas:
        logger.warning("Configuración de YouTube: %s", aviso)
    if not problemas:
        logger.info(
            "Configuración de YouTube correcta (worker: %s, cookies: %s, proxy: %s).",
            "sí" if YOUTUBE_WORKER_URL else "no",
            "sí" if YOUTUBE_COOKIES_FILE else "no",
            "sí" if YOUTUBE_PROXY else "no",
        )
    return problemas


def _cookiefile() -> str | None:
    """Ruta a la copia escribible del cookies.txt, o None si no hay cookies configuradas."""
    global _cookies_workfile, _cookies_prepared

    if not YOUTUBE_COOKIES_FILE:
        return None

    # downloader.py se llama desde varios hilos (run_in_executor): la copia se hace una
    # sola vez aunque entren varias descargas a la vez.
    with _cookies_lock:
        if _cookies_prepared:
            return _cookies_workfile
        _cookies_prepared = True

        if not os.path.exists(YOUTUBE_COOKIES_FILE):
            logger.warning(
                "YOUTUBE_COOKIES_FILE apunta a una ruta que no existe; se sigue sin cookies.",
            )
            return None
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            dest = os.path.join(DOWNLOAD_DIR, ".youtube-cookies.txt")
            shutil.copyfile(YOUTUBE_COOKIES_FILE, dest)
            os.chmod(dest, 0o600)
            _cookies_workfile = dest
            logger.info("Cookies de YouTube cargadas en una copia de trabajo escribible.")
        except OSError:
            # Nunca se loguea el contenido ni la ruta del secreto: son credenciales.
            logger.exception("No pude preparar la copia de las cookies; se sigue sin ellas.")
        return _cookies_workfile


# ---------------------------------------------------------------------------
# Worker remoto de YouTube
#
# Un servicio idéntico a este código corriendo en una máquina con IP residencial
# (worker.py, publicado por un túnel). Se le delegan SOLO los links de YouTube: es la
# única plataforma que bloquea al host por ser datacenter.
#
# La regla de oro es que el worker nunca puede empeorar el servicio. Si no contesta
# —máquina apagada, túnel caído, timeout— se sigue por el camino local, que es
# exactamente lo que pasaba antes de que el worker existiera. Y para que estar apagado
# no cueste un timeout por cada link, el primer fallo abre un cooldown durante el cual
# ni se intenta.
# ---------------------------------------------------------------------------
_worker_lock = threading.Lock()
_worker_down_until = 0.0


def _worker_enabled(url_is_youtube: bool) -> bool:
    """True si hay worker configurado, el link es de YouTube y no está en cooldown."""
    if not url_is_youtube:
        return False
    if not YOUTUBE_WORKER_URL:
        logger.info("YouTube sin worker configurado: se resuelve en local.")
        return False
    with _worker_lock:
        restante = _worker_down_until - time.monotonic()
    if restante > 0:
        # Sin esta línea, los links que caen dentro de la ventana de cooldown parecen
        # fallar por su cuenta: en realidad ni se intentó el worker.
        logger.warning(
            "Worker en cooldown (%.0fs restantes): este link de YouTube va directo a "
            "local sin intentarlo.", restante,
        )
        return False
    return True


def _worker_fail_desc(err: Exception) -> str:
    """
    Por qué falló la llamada al worker, en corto. Solo tipos y códigos de estado:
    nunca la URL (es el dominio de casa de alguien) ni el token.

    El código importa para el diagnóstico: un 524 es el edge del túnel cortando la
    espera de cabeceras —el worker sigue trabajando, pero ya nadie escucha— y no
    tiene nada que ver con que el worker esté caído.
    """
    if isinstance(err, urllib.error.HTTPError):
        return f"HTTP {err.code}"
    reason = getattr(err, "reason", None)
    if reason is not None:
        return f"{type(err).__name__}/{type(reason).__name__}"
    return type(err).__name__


def _worker_unreachable(err: Exception) -> None:
    global _worker_down_until
    with _worker_lock:
        _worker_down_until = time.monotonic() + YOUTUBE_WORKER_COOLDOWN
    # Nunca se loguea la URL ni el token del worker: el token es un secreto y la URL
    # es el dominio de casa de alguien.
    logger.warning(
        "El worker de YouTube no respondió (%s). Se sigue en LOCAL, donde la IP del "
        "servidor suele estar bloqueada por YouTube: si el próximo error habla de un "
        "chequeo antibot, la causa real es esta línea, no YouTube. No se reintenta "
        "durante %d s.", _worker_fail_desc(err), YOUTUBE_WORKER_COOLDOWN,
    )


class _WorkerRejected(Exception):
    """El worker respondió, pero yt-dlp falló allá (video privado, borrado...)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _worker_call(path: str, payload: dict):
    """
    POST al worker. Devuelve la respuesta abierta (el que llama la cierra).

    Levanta _WorkerRejected si el worker contestó con un error de yt-dlp: eso NO es
    caerse, es una respuesta válida que hay que propagar tal cual — reintentarlo en
    local daría el mismo fallo con peor mensaje (el chequeo antibot en vez de
    "video privado").
    """
    req = urllib.request.Request(
        YOUTUBE_WORKER_URL + path,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Worker-Token": YOUTUBE_WORKER_TOKEN,
            # Anuncia que este servidor entiende el flujo de trabajos. Un worker que no
            # lo conozca ignora la cabecera y responde con el archivo, como siempre.
            "X-Worker-Protocol": "2",
            # Sin esto urllib manda "Python-urllib/3.x", que las protecciones antibot
            # del túnel (Cloudflare y equivalentes) bloquean con un 403 antes de que la
            # petición llegue siquiera a la máquina del worker. Verificado: la misma
            # petición pasa o falla solo por esta cabecera. Se reutiliza el _UA que ya
            # usa el resto del módulo en vez de inventar otro.
            "User-Agent": _UA,
        },
        method="POST",
    )
    # El tiempo hasta las cabeceras es EL dato de diagnóstico: /video no manda un byte
    # hasta terminar de bajar el video entero, así que este número es la duración de la
    # descarga en la conexión del worker. Comparado con el límite del túnel dice si lo
    # cortaron esperando (corte del edge) o si expiró nuestro propio timeout.
    started = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=YOUTUBE_WORKER_TIMEOUT)
    except urllib.error.HTTPError as e:
        elapsed = time.monotonic() - started
        if e.code == 422:  # el worker corrió yt-dlp y yt-dlp falló
            try:
                detail = json.loads(e.read().decode()).get("detail") or ""
            except Exception:
                detail = ""
            # Distinguir esto de una caída es el objetivo de todo el logging: acá el
            # worker SÍ trabajó y fue YouTube quien le dijo que no, o sea que el
            # problema está en la conexión del worker, no en el transporte.
            logger.warning(
                "Worker %s: yt-dlp falló EN EL WORKER tras %.1fs — %s",
                path, elapsed, detail or "(sin detalle)",
            )
            raise _WorkerRejected(detail or "No pude descargar ese contenido.") from e
        logger.warning("Worker %s: respondió HTTP %d tras %.1fs.", path, e.code, elapsed)
        raise  # 5xx, 401, etc.: el worker está mal, se trata como caída
    except Exception as e:
        logger.warning(
            "Worker %s: sin respuesta tras %.1fs (%s). Timeout propio: %ds.",
            path, time.monotonic() - started, _worker_fail_desc(e), YOUTUBE_WORKER_TIMEOUT,
        )
        raise
    logger.info(
        "Worker %s: cabeceras en %.1fs (HTTP %s).", path, time.monotonic() - started, resp.status,
    )
    return resp


def _worker_info(path: str, payload: dict) -> dict | None:
    """Metadatos vía worker, o None si no contestó (el que llama sigue en local)."""
    try:
        with _worker_call(path, payload) as resp:
            return json.loads(resp.read().decode())
    except _WorkerRejected as e:
        raise yt_dlp.DownloadError(e.message) from e
    except Exception as e:
        _worker_unreachable(e)
        return None


# Cada cuánto se pregunta por un trabajo del worker. Corto para no añadir latencia
# perceptible a las descargas rápidas, y suficientemente espaciado para que sondear
# durante varios minutos no genere tráfico apreciable.
_WORKER_POLL_SECONDS = 2.0


def _worker_get(path: str):
    """GET autenticado al worker. Mismas cabeceras que el POST, User-Agent incluido."""
    req = urllib.request.Request(
        YOUTUBE_WORKER_URL + path,
        headers={"X-Worker-Token": YOUTUBE_WORKER_TOKEN, "User-Agent": _UA},
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=YOUTUBE_WORKER_TIMEOUT)


def _worker_await_job(path: str, job_id: str):
    """
    Sondea un trabajo hasta que esté listo y devuelve la respuesta abierta del archivo.

    Ninguna de estas peticiones dura más que un intercambio corto, que es justamente el
    motivo de todo el esquema: esperar la descarga entera dentro de una sola petición
    hacía que el túnel la cancelara antes de que el worker pudiera contestar.
    """
    started = time.monotonic()
    while True:
        try:
            with _worker_get(f"/jobs/{job_id}") as r:
                estado = json.loads(r.read().decode())
        except Exception as e:
            _worker_unreachable(e)
            return None

        if estado.get("status") == "ready":
            logger.info("Worker %s: trabajo listo tras %.1fs, recogiendo el archivo.",
                        path, time.monotonic() - started)
            break
        if estado.get("status") == "error":
            # Equivale al 422 del esquema anterior: el worker trabajó y yt-dlp falló allá.
            detalle = estado.get("error") or "No pude descargar ese contenido."
            logger.warning("Worker %s: yt-dlp falló EN EL WORKER tras %.1fs — %s",
                           path, time.monotonic() - started, detalle)
            raise _WorkerRejected(detalle)

        if time.monotonic() - started > YOUTUBE_WORKER_TIMEOUT:
            logger.warning("Worker %s: el trabajo seguía en curso tras %ds; se abandona.",
                           path, YOUTUBE_WORKER_TIMEOUT)
            _worker_unreachable(TimeoutError("el trabajo del worker no terminó a tiempo"))
            return None
        time.sleep(_WORKER_POLL_SECONDS)

    try:
        return _worker_get(f"/jobs/{job_id}/file")
    except Exception as e:
        _worker_unreachable(e)
        return None


def _worker_download(path: str, payload: dict) -> tuple[str, dict] | None:
    """
    Descarga vía worker: guarda el archivo que devuelve en DOWNLOAD_DIR y lo entrega
    con los metadatos que vengan en la cabecera. None si el worker no contestó.
    """
    try:
        resp = _worker_call(path, payload)
    except _WorkerRejected as e:
        raise yt_dlp.DownloadError(e.message) from e
    except Exception as e:
        _worker_unreachable(e)
        return None

    # Worker nuevo: acepta el trabajo y devuelve un id al instante (202). Worker viejo:
    # el cuerpo de esta misma respuesta YA es el archivo. Se admiten los dos para que
    # actualizar el servidor antes que el worker (o al revés) no rompa nada.
    if resp.status == 202:
        try:
            with resp:
                job_id = json.loads(resp.read().decode()).get("job")
        except Exception as e:
            _worker_unreachable(e)
            return None
        if not job_id:
            logger.warning("Worker %s: aceptó el trabajo pero no devolvió id.", path)
            return None
        try:
            resp = _worker_await_job(path, job_id)
        except _WorkerRejected as e:
            # Mismo contrato que en el esquema anterior: un fallo de yt-dlp en el worker
            # se propaga como DownloadError en vez de reintentarse en local, donde daría
            # el chequeo antibot en lugar del motivo real.
            raise yt_dlp.DownloadError(e.message) from e
        if resp is None:
            return None

    dest = None
    started = time.monotonic()
    try:
        with resp:
            ext = os.path.splitext(resp.headers.get("X-Filename") or "")[1] or ".mp4"
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            dest = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}{ext}")
            # Por bloques: el archivo puede ser de cientos de MB y el host tiene 512.
            with open(dest, "wb") as fh:
                shutil.copyfileobj(resp, fh, 1024 * 256)
            meta = json.loads(resp.headers.get("X-Meta") or "{}")
    except Exception as e:
        # Cortó a mitad de la transferencia: no dejar el archivo a medias en disco.
        # Cuántos MB habían llegado separa "nunca empezó" de "murió a mitad de camino",
        # que apuntan a sitios distintos: lo primero al túnel, lo segundo a la subida.
        parcial = os.path.getsize(dest) if dest and os.path.exists(dest) else 0
        logger.warning(
            "Worker %s: la transferencia se cortó tras %.1fs con %.1f MB recibidos (%s).",
            path, time.monotonic() - started, parcial / 1024 / 1024, _worker_fail_desc(e),
        )
        if dest and os.path.exists(dest):
            os.remove(dest)
        _worker_unreachable(e)
        return None
    logger.info(
        "Worker %s: archivo completo, %.1f MB en %.1fs de transferencia.",
        path, os.path.getsize(dest) / 1024 / 1024, time.monotonic() - started,
    )
    return dest, meta


def _base_opts(youtube: bool = False) -> dict:
    """
    Opciones comunes a toda llamada a yt-dlp: sin logging propio y el mismo User-Agent
    en todas partes. `youtube` lo decide cada función que recibe la URL (o download_song,
    que siempre busca en YouTube), porque el proxy solo se aplica ahí.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {"User-Agent": _UA},
    }
    # Las cookies son por dominio: un cookies.txt de YouTube no se manda a TikTok ni a
    # Instagram, así que ponerlo acá cubre las cinco funciones sin filtrarlo a otras redes.
    cookies = _cookiefile()
    if cookies:
        opts["cookiefile"] = cookies
    # Los clientes de YouTube van acá y no en las funciones de descarga porque el
    # preflight tiene que fallar o funcionar exactamente igual que la descarga real:
    # si el preflight extrajera con un cliente distinto, aprobaría links que después
    # no se pueden bajar. La clave es por extractor, así que no afecta a las otras redes.
    if YOUTUBE_PLAYER_CLIENTS:
        opts["extractor_args"] = {"youtube": {"player_client": list(YOUTUBE_PLAYER_CLIENTS)}}
    # El proxy lleva usuario y clave: no se loguea nunca, ni siquiera al fallar.
    if youtube and YOUTUBE_PROXY:
        opts["proxy"] = YOUTUBE_PROXY
    return opts


def _download_opts(
    output_template: str,
    on_progress: Callable[[str], None] | None,
    youtube: bool = False,
    **extra,
) -> dict:
    """
    Opciones compartidas por las cuatro funciones que descargan de verdad (a diferencia
    de un preflight con download=False): plantilla de salida, tope de tamaño, reintentos
    de red y el hook de progreso. `extra` añade o sobreescribe lo específico de cada una
    (format, postprocessors, extractor_args...).
    """
    def _progress_hook(d: dict) -> None:
        if on_progress:
            on_progress(d.get("status", ""))

    opts = _base_opts(youtube)
    opts.update({
        "outtmpl": output_template,
        "max_filesize": MAX_DOCUMENT_SIZE_BYTES,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [_progress_hook],
    })
    # extractor_args se fusiona por extractor en vez de reemplazarse: la base trae los
    # clientes de YouTube y cada función añade lo suyo ({"tiktok": ...}, {"youtube":
    # {"skip": ...}}). Un update() plano borraría los clientes en cuanto una llamada
    # pasara su propio extractor_args, y solo en esa ruta — un bug difícil de ver.
    extra_args = extra.pop("extractor_args", None)
    opts.update(extra)
    if extra_args:
        merged = {name: dict(args) for name, args in opts.get("extractor_args", {}).items()}
        for name, args in extra_args.items():
            merged.setdefault(name, {}).update(args)
        opts["extractor_args"] = merged
    return opts


# Cada plataforma nombra el H.264 distinto: TikTok reporta vcodec="h264", Instagram y
# YouTube "avc1.<perfil>". Un filtro de prefijo ([vcodec^=avc]) solo pilla el segundo, así
# que las ramas "preferir H.264" se saltaban en TikTok y acababa eligiendo H.265.
_AVC = r"~='^(avc|h264)'"
# El mismo criterio que _AVC, para poder aplicarlo en Python (ver _avc_matches_best).
_AVC_RE = re.compile(r"^(avc|h264)")


def _video_format(best_quality: bool = False) -> str:
    """
    Cadena de selección de formato, ordenada de "no requiere recodificar" a "lo que haya".

    Bajar H.264 directamente es lo único que evita pasar por _ensure_h264, y esa
    recodificación es justo lo que revienta en hosts con poca CPU/RAM: un TikTok 1080p en
    H.265 obliga a un libx264 a 1080p (~760 MB de pico medidos) que en Render free se
    queda colgado o lo mata el OOM. Por eso el códec pesa más que la resolución: vale más
    un 540p que se entrega que un 1080p que nunca llega.

    best_quality invierte esa prioridad: el usuario pidió "máxima calidad" a sabiendas,
    así que manda la resolución y el recode se acepta como costo (ver _ensure_h264).

    Ninguna rama filtra por altura: el tope de resolución vive entero en format_sort
    (ver _format_sort), que es el único sitio donde se puede expresar sin romperse en
    vertical.
    """
    if best_quality:
        # Sin filtro de códec: en TikTok el único 1080p es H.265 (medido: el mejor H.264
        # que publica es un 720p), y el 1440p/2160p de YouTube solo existe en VP9/AV1. Lo
        # que baje en un códec que Telegram no reproduce lo arregla _ensure_h264.
        return "bestvideo+bestaudio/best"
    return (
        # 1. DASH en H.264 (YouTube): video + audio por separado.
        f"bestvideo[vcodec{_AVC}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[vcodec{_AVC}]+bestaudio"
        # 2. Combinado en H.264 (TikTok, Instagram). Instagram sirve el H.264 solo así:
        #    sus streams DASH son VP9, que Telegram no reproduce (imagen congelada + audio).
        f"/best[vcodec{_AVC}][ext=mp4]/best[vcodec{_AVC}]"
        # 3. Sin H.264 disponible: bajar lo que haya y dejar que _ensure_h264 lo arregle.
        f"/best[ext=mp4]/bestvideo+bestaudio/best"
    )


def _format_sort(h: int, best_quality: bool) -> list[str]:
    """
    Orden de formatos, con el tope de resolución incluido como límite ("res:720").

    El tope va aquí y no en un filtro [height<={h}] porque "height" es el lado LARGO del
    video, y en una plataforma vertical eso no es la resolución que se anuncia: el 1080p
    de TikTok es 1080x1920, o sea height=1920. Con el filtro, pedir 1080p (o 480p, o
    cualquier cosa) descartaba TODOS los formatos menos el 540p, y la descarga terminaba
    cayendo en las ramas sin filtro: el ajuste de /settings no cambiaba nada en TikTok ni
    en Reels. El límite de format_sort compara contra el lado corto, que es lo que la
    gente llama "1080p", y además no excluye: si no hay nada bajo el tope, cae en lo más
    cercano en vez de quedarse sin formatos.

    vcodec:avc sigue delante del tope en el modo normal por el mismo motivo que ordena
    _video_format: preferir H.264 aunque sea de mayor resolución evita el recode, que es
    lo que revienta en un host con poca RAM.

    En modo máxima calidad manda la resolución, pero vcodec:avc va justo detrás y ANTES
    que "br": a igualdad de resolución, el H.264 se entrega tal cual y el otro habría que
    recodificarlo, así que un bitrate mayor no compensa. Ese orden importa sobre todo en
    Instagram, que publica el mismo Reel como un combinado H.264 y como streams DASH en
    VP9 de más bitrate: con "br" delante se elegía el VP9 y cada Reel terminaba en un
    recode que en Render free no cabe en memoria. Solo se paga la conversión cuando el
    códec incompatible aporta resolución de verdad, que es lo que el modo promete.
    """
    if best_quality:
        return [f"res:{h}", "fps", "vcodec:avc", "br", "ext:mp4", "acodec:m4a"]
    return ["vcodec:avc", f"res:{h}", "fps", "ext:mp4", "acodec:m4a"]


def _format_opts(h: int, best_quality: bool) -> dict:
    """Selector + orden de formatos, que preflight y descarga tienen que compartir."""
    return {
        "format": _video_format(best_quality),
        "format_sort": _format_sort(h, best_quality),
    }


def _is_image_entry(entry: dict) -> bool:
    """
    True si el entry es una foto: Instagram (y otras redes) exponen las imágenes solo
    como thumbnails, sin formatos de video/audio descargables.

    YouTube nunca publica fotos: si un link de YouTube llega sin "formats" es un fallo
    de extracción (throttling, bot-detection, restricción de formato), nunca un post de
    imagen real. Sin este chequeo, ese fallo se confundía con un post de foto y el video
    se entregaba como si fuera una imagen (el thumbnail en vez del video real).
    """
    extractor = (entry.get("extractor_key") or entry.get("extractor") or "").lower()
    if extractor.startswith("youtube"):
        return False
    if entry.get("formats") or entry.get("url") or entry.get("requested_downloads"):
        return False
    return bool(entry.get("thumbnails"))


def _best_thumbnail_url(entry: dict) -> str | None:
    """URL del thumbnail de mayor resolución (= la foto full-size en posts de imagen)."""
    thumbs = entry.get("thumbnails") or []
    if not thumbs:
        return None
    # Si hay dimensiones, elige la mayor; si no, yt-dlp los ordena de peor a mejor.
    if any((t.get("width") or 0) * (t.get("height") or 0) for t in thumbs):
        best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    else:
        best = thumbs[-1]
    return best.get("url")


def _download_image(url: str) -> str | None:
    """Descarga una imagen (foto de post) a DOWNLOAD_DIR. Devuelve la ruta o None si falla."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    out = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.jpg")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        with open(out, "wb") as f:
            f.write(data)
        return out
    except Exception:
        logger.exception("No se pudo descargar la imagen del post")
        if os.path.exists(out):
            os.remove(out)
        return None


def fetch_thumbnail(url: str, max_bytes: int = 5 * 1024 * 1024) -> bytes | None:
    """
    Descarga los bytes de un thumbnail para previsualización (no los guarda en disco,
    a diferencia de _download_image: el canal web los embebe directo en la respuesta).
    None si falla o si supera max_bytes (tope de seguridad, no debería tocarse nunca
    con thumbnails reales).
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            logger.warning("Thumbnail de previsualización excede %d bytes, se descarta", max_bytes)
            return None
        return data
    except Exception:
        logger.warning("No se pudo descargar el thumbnail de previsualización")
        return None


# Pixel formats que Telegram/iOS reproducen sin problema (8-bit 4:2:0). Otros como
# yuv420p10le (10-bit) o yuv444p producen perfiles H.264 que se ven negros/congelados.
_COMPATIBLE_PIX_FMTS = {"yuv420p", "yuvj420p", "nv12"}

# ffprobe reporta las imágenes como un "stream de video" con estos códecs. No son
# video real: recodificarlas convertiría una foto en un MP4 de 1 frame.
_IMAGE_CODECS = {"mjpeg", "png", "gif", "bmp", "tiff", "webp"}


def _video_stream_info(filepath: str) -> tuple[str | None, str | None]:
    """(codec_name, pix_fmt) del stream de video según ffprobe, o (None, None) si falla."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,pix_fmt", "-of", "default=noprint_wrappers=1", filepath],
            capture_output=True, text=True, timeout=10,
        )
        info: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        return info.get("codec_name") or None, info.get("pix_fmt") or None
    except Exception:
        return None, None


def _video_codec(filepath: str) -> str | None:
    """Nombre del códec de video según ffprobe (p. ej. 'h264', 'vp9'), o None si falla."""
    return _video_stream_info(filepath)[0]


def _has_audio_stream(filepath: str) -> bool:
    """True si el archivo tiene al menos un stream de audio, según ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=10,
        )
        return bool(result.stdout.strip())
    except Exception:
        return True  # si ffprobe falla, no bloqueamos la entrega por un chequeo que no pudo correr


def _warn_if_silent(filepath: str, url: str, max_height: int | None) -> None:
    """
    Red de seguridad: el selector de formato prioriza audio siempre que hay una
    alternativa combinada disponible, pero TikTok (sobre todo con música con licencia
    restringida) a veces solo ofrece un stream sin audio, y esto puede variar entre
    requests al mismo video. No hay forma de "arreglarlo" del lado del cliente — esto
    deja rastro en los logs para poder correlacionar en vez de entregarlo en silencio
    sin ninguna señal de qué pasó.
    """
    if not _has_audio_stream(filepath):
        logger.warning(
            "%s se descargó sin pista de audio (url=%s, max_height=%s). Puede ser una "
            "restricción de la plataforma origen (audio con licencia restringida) o "
            "variación del CDN entre requests al mismo video, no necesariamente un bug "
            "del selector de formato.",
            filepath, url, max_height,
        )


# Resumen final de ebur128 (con framelog=quiet son las únicas líneas que quedan).
_LOUDNESS_I_RE = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.M)
_LOUDNESS_PEAK_RE = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", re.M)
# Por encima de esto no es un archivo "bajo de volumen", es uno prácticamente mudo
# (ebur128 mide -70 LUFS en el silencio): subirlo 50 dB solo amplificaría el ruido.
_MAX_GAIN_DB = 30.0
# Techo del limitador, en dBFS. No es -1 porque el encoder AAC se pasa hasta ~1 dB del
# pico que ve el limitador, y un pico por encima de 0 dBFS suena a distorsión.
_LIMITER_CEILING_DB = -3.0


def _measure_loudness(filepath: str) -> tuple[float, float] | None:
    """
    Mide (loudness integrada, true peak) del audio, en LUFS y dBFS.
    Devuelve None si la medición no sirve: sin audio, silencio, o ffmpeg falló.

    Se mide con ebur128 y con -vn. Las dos cosas importan en un host lento: la primera
    pasada de loudnorm cuesta 5 veces más que ebur128 para el mismo dato, y sin -vn
    ffmpeg decodifica además el video entero para nada. Medido sobre un TikTok de 37 s:
    0,98 s de CPU con loudnorm contra 0,20 s con ebur128, y 2,4 s si encima se deja
    entrar el video — que en Render free (0,1 vCPU) son 24 s de "Procesando".
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-i", filepath,
        "-vn", "-af", "ebur128=peak=true:framelog=quiet",
        "-f", "null", "-",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:
        logger.warning("_measure_loudness: ffmpeg no pudo medir %s", filepath)
        return None
    if r.returncode != 0:
        return None

    err = r.stderr.decode(errors="replace")
    i_match = _LOUDNESS_I_RE.search(err)
    peak_match = _LOUDNESS_PEAK_RE.search(err)
    if not i_match or not peak_match:
        return None
    integrated, peak = float(i_match.group(1)), float(peak_match.group(1))
    # Un archivo mudo mide -70 LUFS o menos; no hay nada que normalizar ahí.
    if integrated != integrated or integrated <= -70.0:
        return None
    return integrated, peak


def _normalize_audio(filepath: str, on_progress: Callable[[str], None] | None = None) -> str:
    """
    Sube el volumen del archivo hasta AUDIO_TARGET_LUFS cuando viene demasiado bajo.

    TikTok (y en menor medida el resto) no dejan el volumen horneado en el archivo: la
    app aplica la ganancia al reproducir con la loudness que manda su API, así que el
    MP4 descargado suena mucho más flojo que el mismo video dentro de la app — medido,
    -27.3 LUFS contra los -14 LUFS de referencia, o sea unos 13 dB por debajo.

    Solo se recodifica la pista de audio (el video se copia), así que no entra libx264
    y el pico de RAM no se mueve. Si la medición o ffmpeg fallan se devuelve el archivo
    original: un audio bajo es mucho mejor que una descarga perdida.
    """
    if not NORMALIZE_AUDIO or not _has_audio_stream(filepath):
        return filepath

    measured = _measure_loudness(filepath)
    if measured is None:
        return filepath
    integrated, peak = measured

    gain = AUDIO_TARGET_LUFS - integrated
    if gain < AUDIO_MIN_GAIN_LU or gain > _MAX_GAIN_DB:
        logger.info("_normalize_audio: %s está en %.1f LUFS (ganancia %.1f dB), no se toca",
                    filepath, integrated, gain)
        return filepath

    out = filepath.rsplit(".", 1)[0] + "_norm.mp4"
    logger.info("_normalize_audio: %s a %.1f LUFS (pico %.1f dBFS) +%.1f dB → %.1f LUFS",
                filepath, integrated, peak, gain, AUDIO_TARGET_LUFS)
    if on_progress:
        on_progress("normalizing")
    # Ganancia fija más limitador, en vez de una segunda pasada de loudnorm: cuesta 0,45 s
    # de CPU contra 1,7 s para el mismo resultado medido (-15,8 LUFS contra -15,1), y en un
    # host de 0,1 vCPU esa diferencia son 12 s de espera. El limitador es lo que permite
    # subir 12 dB un audio cuyo pico ya estaba a -4,7 dBFS sin clipear.
    ceiling = 10 ** (_LIMITER_CEILING_DB / 20)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-i", filepath,
        "-map", "0",
        "-c:v", "copy",
        "-af", f"volume={gain:.1f}dB,alimiter=limit={ceiling:.3f}:level=disabled",
        *_ffmpeg_threads(),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
    except Exception:
        logger.warning("_normalize_audio: ffmpeg no terminó sobre %s", filepath)
        if os.path.exists(out):
            os.remove(out)
        return filepath
    if r.returncode != 0 or not os.path.exists(out):
        logger.error("_normalize_audio falló (rc=%s): %s", r.returncode, r.stderr.decode()[:400])
        if os.path.exists(out):
            os.remove(out)
        return filepath

    os.remove(filepath)
    return out


def _ffmpeg_threads() -> list[str]:
    """Args de hilos para ffmpeg. Ver FFMPEG_THREADS: es un límite de memoria."""
    return ["-threads", str(FFMPEG_THREADS)] if FFMPEG_THREADS else []


# Las recodificaciones no pueden solaparse aunque sí lo hagan las descargas. Con
# MAX_CONCURRENT_DOWNLOADS=2, dos libx264 a la vez suman ~640 MB y en un host de 512 MB
# el segundo se lleva al proceso entero por OOM — y el usuario ve la descarga colgada
# para siempre, porque el mensaje de estado se queda donde estaba. Serializarlos hace
# esperar al segundo, que es infinitamente mejor que matar a los dos. El semáforo vive
# aquí, en el motor, porque el límite es del host: lo comparten todos los canales.
_encode_lock = threading.Semaphore(1)


def transcode_allowed(duration: float | None) -> bool:
    """
    ¿Cabe recodificar un video de esta duración en este host? Ver TRANSCODE_MAX_DURATION.

    Una duración desconocida se trata como que no cabe cuando hay límite: si no se puede
    medir, tampoco se puede prometer que termine, y el precio de equivocarse es tener al
    usuario cinco minutos esperando algo que va a fallar.
    """
    if not TRANSCODE_MAX_DURATION:
        return False
    if duration is None:
        return TRANSCODE_MAX_DURATION >= 3600
    return duration <= TRANSCODE_MAX_DURATION


class VideoConversionError(Exception):
    """
    El video bajó bien pero no se pudo dejar en un formato reproducible.

    Antes esto se tragaba y se entregaba el archivo original: para un H.265/VP9/AV1 eso
    significa que Telegram muestra la imagen congelada con el audio sonando, que es peor
    que un error — el usuario no tiene forma de saber qué pasó ni qué hacer. Cada canal
    lo traduce a un mensaje suyo.
    """


def _ensure_h264(filepath: str, short_side_cap: int | None = None,
                 on_progress: Callable[[str], None] | None = None) -> str:
    """
    Red de seguridad de compatibilidad: Telegram (y iOS/QuickTime) no reproducen VP9/AV1
    dentro de un MP4 — ni los perfiles H.264 de 10-bit / 4:4:4 — se ve la imagen congelada
    mientras el audio suena. Si el video no es H.264 8-bit 4:2:0, lo recodifica copiando el
    audio. No-op en el caso normal (H.264 yuv420p), así que no añade coste en casi ninguna
    descarga.
    """
    codec, pix_fmt = _video_stream_info(filepath)
    if not codec or codec in _IMAGE_CODECS:
        return filepath  # probe falló o es una imagen: no tocar
    codec_ok = codec in ("h264", "avc1")
    pix_ok = pix_fmt is None or pix_fmt in _COMPATIBLE_PIX_FMTS
    if codec_ok and pix_ok:
        return filepath

    duration = _probe_duration(filepath)
    if not transcode_allowed(duration):
        # Se entrega sin convertir: en algunos clientes de Telegram un VP9/AV1 se ve
        # congelado, pero eso es recuperable (la web lo sirve tal cual) y cinco minutos
        # de espera terminando en error no lo son. El canal lo avisa.
        logger.warning(
            "No recodifico %s (codec=%s, %.0fs): supera TRANSCODE_MAX_DURATION=%ss. "
            "Se entrega sin convertir.",
            filepath, codec, duration or -1, TRANSCODE_MAX_DURATION,
        )
        if on_progress:
            on_progress("incompatible")
        return filepath

    out = filepath.rsplit(".", 1)[0] + "_h264.mp4"
    logger.info("Recodificando %s (codec=%s pix_fmt=%s, %.0fs) → H.264 yuv420p para compatibilidad con Telegram",
                filepath, codec, pix_fmt, duration or -1)
    # Es la etapa más lenta de todas y hasta ahora no se anunciaba: el canal se quedaba
    # en "Procesando" durante todo el recode, que en un host de 0,1 vCPU son minutos.
    if on_progress:
        on_progress("converting")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-v", "error", "-i", filepath]
    if short_side_cap:
        # Modo máxima calidad: el tope se aplica al lado CORTO, que es lo que la gente
        # llama "1080p". Capar la altura como en la rama de abajo dejaría un TikTok de
        # 1080x1920 en 608x1080 — o sea, recodificar caro para entregar menos de lo que
        # ya se bajaba sin el modo. El if elige el lado según la orientación y el -2 del
        # otro lado mantiene el aspecto (y par); min() evita el upscale.
        cmd += ["-vf", f"scale='if(gt(iw,ih),-2,min({short_side_cap},iw))'"
                       f":'if(gt(iw,ih),min({short_side_cap},ih),-2)'"]
    elif MAX_COMPRESS_HEIGHT:
        # La RAM de libx264 escala con la resolución: un encode a 1080p pica en ~700 MB y
        # en un host de 512 MB lo mata el OOM a mitad, dejando la descarga colgada sin
        # error. Mismo cap que compress_video. -2 mantiene el ancho par; nunca hace upscale.
        cmd += ["-vf", f"scale=-2:'min({MAX_COMPRESS_HEIGHT},ih)'"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        *_ffmpeg_threads(),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", out,
    ]
    # Un fallo aquí es casi siempre el OOM killer llevándose a ffmpeg: la RAM de libx264
    # escala con la resolución y en un host de 512 MB un encode a 1080p no entra (medido:
    # ~760 MB de pico en 1080x1920). Ver MAX_QUALITY_COMPRESS_HEIGHT.
    def _failed(reason: str) -> None:
        logger.error("_ensure_h264 no pudo convertir %s (codec=%s): %s", filepath, codec, reason)
        for leftover in (out, filepath):
            if os.path.exists(leftover):
                os.remove(leftover)
        raise VideoConversionError(reason)

    try:
        with _encode_lock:
            r = subprocess.run(cmd, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        _failed("ffmpeg superó el timeout de 300s")
    if r.returncode != 0 or not os.path.exists(out):
        _failed(f"ffmpeg rc={r.returncode}: {r.stderr.decode()[:400]}")
    os.remove(filepath)
    return out


def _fix_stream_loop(filepath: str) -> str:
    """
    Instagram stores Reels with a looping video (e.g. 31s) paired with full-length audio (e.g. 62s).
    Their native app loops the video silently. When merged into a standard MP4, the mismatch causes
    Telegram to show a black second half. This function detects that mismatch and loops the video
    stream to match the audio duration using FFmpeg.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", filepath],
            capture_output=True, text=True, timeout=10,
        )
        streams = json.loads(result.stdout).get("streams", [])
        video_dur = next(
            (float(s["duration"]) for s in streams if s["codec_type"] == "video" and "duration" in s),
            None,
        )
        audio_dur = next(
            (float(s["duration"]) for s in streams if s["codec_type"] == "audio" and "duration" in s),
            None,
        )

        logger.info("stream_loop check: video=%.2fs audio=%.2fs", video_dur or 0, audio_dur or 0)

        if not video_dur or not audio_dur or video_dur >= audio_dur * 0.9:
            return filepath

        logger.info("Applying stream loop fix: looping video %.2fs → %.2fs", video_dur, audio_dur)

        # The concat demuxer uses the container duration to advance to the next entry.
        # Since the merged file has a 62s container (dominated by audio), feeding it
        # directly to concat would never advance to a second entry. Solution: extract
        # the video-only stream first (31s container), then concat that N times, then
        # mux with the original audio.
        video_only = filepath + ".video_only.mp4"
        concat_txt = filepath + ".concat.txt"
        fixed = filepath.rsplit(".", 1)[0] + "_fixed.mp4"
        # Cada rama de error devolvía el original dejando su temporal en disco: el
        # video-only si falla la extracción, ese más el .txt si salta un timeout (las
        # dos líneas de unlink estaban DESPUÉS del subprocess), y el _fixed a medias si
        # falla el concat. Nada los borraba después, porque el pipeline solo conoce la
        # ruta que esta función devuelve. De ahí el finally.
        exito = False
        try:
            r1 = subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-nostats", "-v", "error",
                 "-i", filepath, "-c:v", "copy", "-an", video_only],
                capture_output=True, timeout=30,
            )
            if r1.returncode != 0:
                logger.error("ffmpeg video extract failed: %s", r1.stderr.decode())
                return filepath

            repeats = math.ceil(audio_dur / video_dur)
            abs_path = os.path.abspath(video_only)
            with open(concat_txt, "w") as f:
                for _ in range(repeats):
                    f.write(f"file '{abs_path}'\n")

            r2 = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-nostats", "-v", "error",
                    "-f", "concat", "-safe", "0", "-i", concat_txt,
                    "-i", filepath,
                    "-map", "0:v:0",
                    "-map", "1:a:0",
                    "-t", str(audio_dur),
                    "-c:v", "copy",
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    fixed,
                ],
                capture_output=True, timeout=120,
            )
            if r2.returncode != 0:
                logger.error("ffmpeg concat failed (rc=%d): %s", r2.returncode, r2.stderr.decode())
                return filepath

            os.remove(filepath)
            exito = True
            logger.info("Stream loop fix applied: %s", fixed)
            return fixed
        finally:
            for tmp in (concat_txt, video_only) if exito else (concat_txt, video_only, fixed):
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        logger.warning("No pude borrar el temporal %s", tmp)
    except Exception:
        logger.exception("_fix_stream_loop failed unexpectedly")
        return filepath


def download_video(url: str, on_progress: Callable[[str], None] | None = None,
                   max_height: int | None = None, best_quality: bool = False) -> str:
    """
    Descarga el video de la URL dada y devuelve la ruta al archivo.
    Lanza yt_dlp.DownloadError si algo falla.
    on_progress recibe el status string de yt-dlp ("downloading", "finished", etc).
    best_quality prioriza resolución sobre códec (ver _video_format).
    """
    h = max_height or MAX_VIDEO_HEIGHT

    if _worker_enabled(is_youtube_url(url)):
        if on_progress:
            on_progress("downloading")
        # Un worker viejo ignora best_quality (pydantic descarta los campos que no
        # conoce) y devuelve la calidad de siempre: se degrada, no se rompe.
        got = _worker_download("/video", {"url": url, "max_height": h, "best_quality": best_quality})
        if got:
            # El worker ya corrió _ensure_h264 y _fix_stream_loop de su lado: lo que
            # llega es el archivo final, no hay que volver a procesarlo.
            return got[0]

    output_template = _make_output_path()

    ydl_opts = _download_opts(
        output_template, on_progress, youtube=is_youtube_url(url),
        **_format_opts(h, best_quality),
        merge_output_format="mp4",
        postprocessor_args={
            "merger": [
                "-c:v", "copy",
                "-c:a", "copy",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
            ],
        },
        extractor_args={"tiktok": {"webpage_download": True}},
    )

    def _do_download() -> str:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not os.path.exists(filename):
                filename = filename.rsplit(".", 1)[0] + ".mp4"
        return filename

    filename = _run_with_retry(_do_download)
    try:
        filename = _ensure_h264(filename, MAX_QUALITY_COMPRESS_HEIGHT if best_quality else None,
                                on_progress=on_progress)
    except VideoConversionError:
        if not best_quality:
            raise
        # En máxima calidad el recode es parte del trato, así que fallar ahí no puede
        # costarle el video al usuario: se vuelve a bajar con el selector normal, que
        # prefiere H.264 y no necesita convertir nada. Es lo que habría recibido sin el
        # modo. No hay riesgo de bucle: la segunda pasada va con best_quality=False y
        # ahí la excepción se propaga.
        logger.warning("Recode de máxima calidad fallido en %s: se reintenta en el "
                       "formato compatible", url)
        if on_progress:
            on_progress("fallback")
        return download_video(url, on_progress, max_height, best_quality=False)
    filename = _fix_stream_loop(filename)
    # Después de los remuxes: ambos copian el audio tal cual, así que normalizar antes
    # sería medir un archivo que todavía puede cambiar de pista.
    filename = _normalize_audio(filename, on_progress=on_progress)
    _warn_if_silent(filename, url, max_height)
    return filename


def download_post(
    url: str,
    on_progress: Callable[[str], None] | None = None,
    max_height: int | None = None,
    best_quality: bool = False,
) -> list[dict]:
    """
    Descarga todos los items de un post con varios elementos (carrusel de Instagram, etc.).
    Devuelve una lista de {"path": str, "kind": "photo" | "video"} en orden.
    Lanza yt_dlp.DownloadError si algo falla.
    """
    output_template = _make_carousel_template()
    h = max_height or MAX_VIDEO_HEIGHT

    ydl_opts = _download_opts(
        output_template, on_progress, youtube=is_youtube_url(url),
        # Formato permisivo: los items de video bajan con el mismo selector que
        # download_video, y los de imagen caen al único formato disponible (la foto).
        **_format_opts(h, best_quality),
        merge_output_format="mp4",
        # Los items de foto no tienen formato de video: sin esto yt-dlp lanza
        # "No video formats found!" y aborta el post entero. Las fotos se bajan aparte.
        ignore_no_formats_error=True,
    )

    def _do_download() -> list[dict]:
        items: list[dict] = []
        try:
            return _collect_items(ydl_opts, url, items, max_height, best_quality, on_progress)
        except Exception:
            # Los archivos ya escritos no llegan a manos de nadie: el `finally` de
            # pipeline.carousel limpia la lista que esta función devuelve, y al propagar
            # no devuelve ninguna. Sin esto, un post que revienta a mitad deja en disco
            # todo lo que ya había bajado.
            for it in items:
                path = it.get("path")
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        logger.warning("No pude borrar el item huérfano %s", path)
            raise

    def _collect_items(ydl_opts: dict, url: str, items: list[dict], max_height, best_quality,
                       on_progress) -> list[dict]:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Extraer sin descargar: con download=True yt-dlp intenta bajar cada item y
            # revienta en las fotos (no tienen formato). Bajamos cada item por separado.
            info = ydl.extract_info(url, download=False)
            entries = info.get("entries") or [info]
            for entry in entries:
                if not entry:
                    continue
                # Foto: Instagram la expone solo como thumbnail. Bajarla directamente.
                if _is_image_entry(entry):
                    thumb = _best_thumbnail_url(entry)
                    img = _download_image(thumb) if thumb else None
                    if img:
                        items.append({"path": img, "kind": "photo"})
                    else:
                        logger.warning("Foto de carrusel sin thumbnail utilizable: %s", entry.get("id"))
                    continue
                # Video: descargarlo con yt-dlp a partir del entry ya extraído.
                try:
                    processed = ydl.process_ie_result(entry, download=True)
                except yt_dlp.DownloadError:
                    logger.warning("No se pudo descargar item de video del carrusel: %s", entry.get("id"))
                    continue
                path = _entry_filepath(processed)
                if not path or not os.path.exists(path):
                    logger.warning("Item de video sin archivo en disco: %s", entry.get("id"))
                    continue
                ext = path.rsplit(".", 1)[-1].lower()
                kind = "photo" if ext in _IMAGE_EXTS else "video"
                # Misma red de seguridad que download_video: un item de video en VP9/AV1
                # (o H.264 10-bit) se vería congelado en el álbum de Telegram.
                if kind == "video":
                    try:
                        path = _ensure_h264(path, MAX_QUALITY_COMPRESS_HEIGHT if best_quality else None,
                                            on_progress=on_progress)
                    except VideoConversionError:
                        # Mismo criterio que un item que no se pudo bajar: se salta y el
                        # resto del álbum se entrega igual. Colarlo sin convertir dejaría
                        # un elemento congelado en medio del carrusel.
                        logger.warning("Item de video no convertible, se omite: %s", entry.get("id"))
                        continue
                    path = _normalize_audio(path, on_progress=on_progress)
                    _warn_if_silent(path, url, max_height)
                items.append({"path": path, "kind": kind})
        return items

    return _run_with_retry(_do_download)


def _format_res(f: dict) -> int:
    """Resolución de un formato por su lado CORTO, que es lo que se anuncia como '1080p'."""
    w, h = f.get("width"), f.get("height")
    if w and h:
        return min(w, h)
    return h or w or 0


def _avc_matches_best(info: dict) -> bool:
    """
    True si el mejor H.264 disponible llega a la misma resolución que el mejor formato
    a secas. Cuando pasa, el modo máxima calidad no tiene nada que ganar convirtiendo.

    Hace falta mirarlo aquí, en Python, porque no se puede expresar en un selector de
    yt-dlp: "bestvideo+bestaudio" se queda con la vía DASH en cuanto existe, y en
    Instagram el DASH es VP9 mientras el H.264 se publica como formato combinado de la
    MISMA resolución. Resultado: cada Reel se bajaba en VP9 y se recodificaba para
    terminar en el mismo 1080x1920 que el combinado daba ya listo — un recode que en un
    host de 512 MB no cabe en memoria.
    """
    videos = [f for f in (info.get("formats") or []) if f.get("vcodec") != "none"]
    if not videos:
        return False
    best = max(_format_res(f) for f in videos)
    avc = [f for f in videos if _AVC_RE.match(f.get("vcodec") or "")]
    return bool(avc) and max(_format_res(f) for f in avc) >= best


def _available_height(info: dict) -> int | None:
    """
    Altura del mejor formato de video que la plataforma publica para este item.
    El canal web la usa para no ofrecer resoluciones que el video no puede dar:
    hasta ahora el desplegable listaba siempre hasta 4K, así que pedir 2160p en un
    TikTok de 1080p era una opción que no significaba nada.

    Se lee de "formats" (la lista completa que devuelve extract_info) y no del
    formato ya seleccionado: el selector de _video_format ya vino recortado por
    max_height, así que preguntarle a él devolvería el tope pedido, no el real.
    """
    formats = info.get("formats") or []
    heights = [
        f.get("height") for f in formats
        # Solo se excluye el "none" explícito, que es audio: hay extractores que no
        # ponen vcodec, y descartar por ausencia dejaba fuera formatos de video reales.
        if f.get("height") and f.get("vcodec") != "none"
    ]
    if heights:
        return max(heights)
    # Sin lista de formatos (algunos extractores devuelven el item ya resuelto)
    # el propio info suele traer la altura del único formato que hay.
    return info.get("height")


def _estimate_filesize(info: dict) -> int | None:
    # Para streams DASH (video+audio separados), suma ambos tamaños
    requested = info.get("requested_formats") or []
    if requested:
        total = sum(
            (f.get("filesize") or f.get("filesize_approx") or 0)
            for f in requested
        )
        return total or None
    return info.get("filesize") or info.get("filesize_approx")


# Marcadores de "audio del propio creador": no es una canción/artista real.
_ORIGINAL_SOUND_MARKERS = (
    "original sound",
    "sonido original",
    "som original",
    "audio original",
    "оригинальный звук",
    "son original",
)


def _identify_song(info: dict) -> dict | None:
    """
    Devuelve {"track", "artist"} si la plataforma tagueó una canción real,
    o None si es 'original sound'/sin datos suficientes.
    """
    track = info.get("track")
    artist = info.get("artist") or (info.get("artists") or [None])[0]
    if not track or not artist:
        return None
    low = track.lower()
    if any(m in low for m in _ORIGINAL_SOUND_MARKERS):
        return None
    return {"track": track, "artist": artist}


def get_video_info(url: str, max_height: int | None = None, best_quality: bool = False) -> dict:
    """
    Obtiene metadatos del video sin descargarlo.
    Retorna title, duration (segundos) y filesize (bytes, puede ser None).
    Lanza yt_dlp.DownloadError si el video no existe o es privado.
    """
    h = max_height or MAX_VIDEO_HEIGHT

    if _worker_enabled(is_youtube_url(url)):
        got = _worker_info("/info", {"url": url, "max_height": h, "best_quality": best_quality})
        if got is not None:
            return got

    opts = _base_opts(is_youtube_url(url))
    opts.update({
        # Mismo selector que la descarga real: si el preflight estimara el tamaño de un
        # formato distinto al que luego se baja, el chequeo de límite no valdría nada.
        # Vale también para best_quality: ahí el formato elegido es otro y pesa distinto.
        **_format_opts(h, best_quality),
        # Necesario para posts de foto (single o carrusel): sin esto el preflight
        # revienta con "No video formats found!" antes de poder clasificarlos.
        "ignore_no_formats_error": True,
    })

    def _extract() -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    # El preflight es donde fallan los errores intermitentes de extracción (ej. la
    # "rehydration" de TikTok), así que también se reintenta.
    info = _run_with_retry(_extract)

    # Post con varios elementos (carrusel): yt-dlp lo devuelve como playlist con entries.
    entries = info.get("entries")
    if entries is not None:
        entries = [e for e in entries if e]
        if len(entries) != 1:
            total = sum((_estimate_filesize(e) or 0) for e in entries)
            return {
                "title": info.get("title") or "Sin título",
                "duration": None,
                "filesize": total or None,
                "is_music": False,
                "is_playlist": True,
                "count": len(entries),
                # El post en sí (no cada entry) suele traer su propio thumbnail; si no
                # lo trae, el canal web simplemente no muestra previsualización.
                "thumbnail": _best_thumbnail_url(info),
            }
        # Un único item envuelto en "playlist": tratarlo como item suelto para poder
        # clasificarlo (foto vs video) y enrutarlo bien.
        info = entries[0]

    return {
        "title": info.get("title") or "Sin título",
        "duration": info.get("duration"),
        "filesize": _estimate_filesize(info),
        "available_height": _available_height(info),
        "is_music": bool(info.get("track") or info.get("artist")),
        "is_playlist": False,
        "is_image": _is_image_entry(info),
        # Para que el canal pueda ahorrarse el modo máxima calidad cuando no aporta nada.
        "avc_matches_best": _avc_matches_best(info),
        "count": 1,
        "song": _identify_song(info),
        "thumbnail": _best_thumbnail_url(info),
    }


def download_audio(url: str, on_progress: Callable[[str], None] | None = None) -> tuple[str, dict]:
    """
    Descarga solo el audio en MP3.
    Devuelve (ruta_mp3, {"title": str, "artist": str | None}).
    Lanza yt_dlp.DownloadError si algo falla.
    """
    if _worker_enabled(is_youtube_url(url)):
        if on_progress:
            on_progress("downloading")
        got = _worker_download("/audio", {"url": url})
        if got:
            return got

    output_template = _make_output_path()

    ydl_opts = _download_opts(
        output_template, on_progress, youtube=is_youtube_url(url),
        format="bestaudio/best",
        postprocessors=[{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
        extractor_args={"youtube": {"skip": ["dash", "hls"]}},
    )

    def _do_download() -> tuple[str, dict]:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            filename = filename.rsplit(".", 1)[0] + ".mp3"

        title = info.get("track") or info.get("title") or "Sin título"
        artist = info.get("artist") or info.get("creator") or None
        return filename, {"title": title, "artist": artist}

    return _run_with_retry(_do_download)


def download_song(query: str, on_progress: Callable[[str], None] | None = None) -> tuple[str, dict]:
    """
    Busca la canción en YouTube (ytsearch1) y la descarga en MP3.
    Devuelve (ruta_mp3, {"title": str, "artist": str | None}).
    Lanza yt_dlp.DownloadError si no encuentra/descarga nada.
    """
    if _worker_enabled(True):
        if on_progress:
            on_progress("downloading")
        got = _worker_download("/song", {"query": query})
        if got:
            return got

    output_template = _make_output_path()

    ydl_opts = _download_opts(
        # ytsearch1 siempre va contra YouTube, aunque acá no haya URL que mirar.
        output_template, on_progress, youtube=True,
        format="bestaudio/best",
        noplaylist=True,
        postprocessors=[{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
    )

    def _do_download() -> tuple[str, dict]:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=True)
            # ytsearch devuelve una playlist; tomamos el primer resultado.
            entry = (info.get("entries") or [info])[0]
            if not entry:
                raise yt_dlp.DownloadError(f"Sin resultados para: {query}")
            filename = ydl.prepare_filename(entry).rsplit(".", 1)[0] + ".mp3"

        title = entry.get("track") or entry.get("title") or query
        artist = entry.get("artist") or entry.get("creator") or entry.get("uploader") or None
        return filename, {"title": title, "artist": artist}

    return _run_with_retry(_do_download)


def get_audio_info(url: str) -> dict:
    """Obtiene metadatos del audio sin descargarlo. Retorna filesize (bytes, puede ser None)."""
    if _worker_enabled(is_youtube_url(url)):
        got = _worker_info("/audio-info", {"url": url})
        if got is not None:
            return got

    opts = _base_opts(is_youtube_url(url))
    opts["format"] = "bestaudio/best"

    def _extract() -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = _run_with_retry(_extract)
    return {
        "filesize": info.get("filesize") or info.get("filesize_approx"),
    }


def get_video_dimensions(filepath: str) -> tuple[int, int]:
    """Devuelve (width, height) del video usando ffprobe. Retorna (0, 0) si falla."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-select_streams", "v:0",
                filepath,
            ],
            capture_output=True, text=True, timeout=10,
        )
        streams = json.loads(result.stdout).get("streams", [])
        if streams:
            return streams[0].get("width", 0), streams[0].get("height", 0)
    except Exception:
        pass
    return 0, 0


def _probe_duration(filepath: str) -> float | None:
    """Duración del archivo en segundos según ffprobe, o None si falla."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", filepath],
            capture_output=True, text=True, timeout=10,
        )
        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception:
        return None


def compress_video(filepath: str, target_bytes: int, max_height: int | None = None) -> str | None:
    """
    Re-codifica el video apuntando a un tamaño <= target_bytes para poder enviarlo
    como video reproducible (en vez de como documento).
    max_height limita la resolución de salida (baja el pico de RAM de libx264 en hosts
    con poca memoria); nunca hace upscale.
    Devuelve la ruta del archivo comprimido, o None si no se pudo bajar lo suficiente.
    """
    duration = _probe_duration(filepath)
    if not duration or duration <= 0:
        return None

    # Presupuesto de bitrate: 95% del objetivo, reservando 128 kbps para audio.
    audio_bps = 128 * 1024
    total_bps = (target_bytes * 8 / duration) * 0.95
    video_bps = int(total_bps - audio_bps)
    if video_bps < 150_000:  # demasiado bajo: la calidad sería inservible
        logger.info("compress_video: bitrate objetivo %d bps muy bajo, se omite", video_bps)
        return None

    out = filepath.rsplit(".", 1)[0] + "_compressed.mp4"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-v", "error", "-i", filepath]
    if max_height:
        # scale=-2:min(h,ih) → baja a max_height solo si el original es más alto
        # (-2 mantiene el ancho par y la relación de aspecto). No hace upscale.
        cmd += ["-vf", f"scale=-2:'min({max_height},ih)'"]
    cmd += [
        "-c:v", "libx264", "-b:v", str(video_bps),
        "-maxrate", str(int(video_bps * 1.5)),
        "-bufsize", str(int(video_bps * 2)),
        "-preset", "veryfast",
        *_ffmpeg_threads(),
        # Fuerza 8-bit 4:2:0: si el origen es 10-bit/4:4:4, sin esto libx264 saca un
        # perfil (High 10/4:4:4) que Telegram muestra negro/congelado.
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out,
    ]

    try:
        with _encode_lock:
            r = subprocess.run(cmd, capture_output=True, timeout=600)
    except Exception:
        logger.exception("compress_video: ffmpeg falló al ejecutar")
        if os.path.exists(out):
            os.remove(out)
        return None

    if r.returncode != 0:
        logger.error("compress_video: ffmpeg rc=%d: %s", r.returncode, r.stderr.decode()[:500])
        if os.path.exists(out):
            os.remove(out)
        return None

    if not os.path.exists(out) or os.path.getsize(out) > target_bytes:
        logger.warning("compress_video: no se alcanzó el objetivo de tamaño")
        if os.path.exists(out):
            os.remove(out)
        return None

    logger.info("compress_video: %d → %d bytes", os.path.getsize(filepath), os.path.getsize(out))
    return out
