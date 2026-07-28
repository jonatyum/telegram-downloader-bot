import json
import logging
import math
import os
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Callable
from typing import TypeVar
import yt_dlp
from config import (
    DOWNLOAD_DIR,
    MAX_DOCUMENT_SIZE_BYTES,
    MAX_VIDEO_HEIGHT,
    MAX_PREFLIGHT_SIZE_BYTES,
    MAX_DOWNLOAD_ATTEMPTS,
    RETRY_BACKOFF_SECONDS,
)

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


def _is_image_entry(entry: dict) -> bool:
    """
    True si el entry es una foto: Instagram (y otras redes) exponen las imágenes solo
    como thumbnails, sin formatos de video/audio descargables.
    """
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


def _ensure_h264(filepath: str) -> str:
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

    out = filepath.rsplit(".", 1)[0] + "_h264.mp4"
    logger.info("Recodificando %s (codec=%s pix_fmt=%s) → H.264 yuv420p para compatibilidad con Telegram",
                filepath, codec, pix_fmt)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", filepath,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", out],
        capture_output=True, timeout=300,
    )
    if r.returncode != 0 or not os.path.exists(out):
        logger.error("_ensure_h264 falló (rc=%s): %s", r.returncode, r.stderr.decode()[:400])
        if os.path.exists(out):
            os.remove(out)
        return filepath
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
        r1 = subprocess.run(
            ["ffmpeg", "-y", "-i", filepath, "-c:v", "copy", "-an", video_only],
            capture_output=True, timeout=30,
        )
        if r1.returncode != 0:
            logger.error("ffmpeg video extract failed: %s", r1.stderr.decode())
            return filepath

        repeats = math.ceil(audio_dur / video_dur)
        concat_txt = filepath + ".concat.txt"
        abs_path = os.path.abspath(video_only)
        with open(concat_txt, "w") as f:
            for _ in range(repeats):
                f.write(f"file '{abs_path}'\n")

        fixed = filepath.rsplit(".", 1)[0] + "_fixed.mp4"
        r2 = subprocess.run(
            [
                "ffmpeg", "-y",
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
        os.unlink(concat_txt)
        os.unlink(video_only)

        if r2.returncode != 0:
            logger.error("ffmpeg concat failed (rc=%d): %s", r2.returncode, r2.stderr.decode())
            return filepath
        os.remove(filepath)
        logger.info("Stream loop fix applied: %s", fixed)
        return fixed
    except Exception:
        logger.exception("_fix_stream_loop failed unexpectedly")
        return filepath


def download_video(url: str, on_progress: Callable[[str], None] | None = None, max_height: int | None = None) -> str:
    """
    Descarga el video de la URL dada y devuelve la ruta al archivo.
    Lanza yt_dlp.DownloadError si algo falla.
    on_progress recibe el status string de yt-dlp ("downloading", "finished", etc).
    """
    output_template = _make_output_path()
    h = max_height or MAX_VIDEO_HEIGHT

    def _progress_hook(d: dict) -> None:
        if on_progress:
            on_progress(d.get("status", ""))

    ydl_opts = {
        "outtmpl": output_template,
        "format": (
            f"bestvideo[vcodec^=avc][height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[vcodec^=avc][height<={h}]+bestaudio"
            # Instagram sirve el H.264 solo como formato combinado; sus streams DASH
            # son VP9, que Telegram no reproduce (imagen congelada + audio). Preferir el
            # combinado antes de fusionar un DASH VP9. best[ext=mp4] cubre los combinados
            # de IG cuya resolución viene sin metadatos (por eso sin filtro de altura).
            f"/best[height<={h}][ext=mp4]/best[ext=mp4]"
            f"/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        ),
        "format_sort": ["res", "fps", "vcodec:avc", "ext:mp4", "acodec:m4a"],
        "merge_output_format": "mp4",
        "postprocessor_args": {
            "merger": [
                "-c:v", "copy",
                "-c:a", "copy",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
            ],
        },
        "extractor_args": {"tiktok": {"webpage_download": True}},
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_DOCUMENT_SIZE_BYTES,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [_progress_hook],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

    def _do_download() -> str:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not os.path.exists(filename):
                filename = filename.rsplit(".", 1)[0] + ".mp4"
        return filename

    filename = _run_with_retry(_do_download)
    filename = _ensure_h264(filename)
    return _fix_stream_loop(filename)


def download_post(
    url: str,
    on_progress: Callable[[str], None] | None = None,
    max_height: int | None = None,
) -> list[dict]:
    """
    Descarga todos los items de un post con varios elementos (carrusel de Instagram, etc.).
    Devuelve una lista de {"path": str, "kind": "photo" | "video"} en orden.
    Lanza yt_dlp.DownloadError si algo falla.
    """
    output_template = _make_carousel_template()
    h = max_height or MAX_VIDEO_HEIGHT

    def _progress_hook(d: dict) -> None:
        if on_progress:
            on_progress(d.get("status", ""))

    ydl_opts = {
        "outtmpl": output_template,
        # Formato permisivo: los items de video bajan en la mejor calidad ≤h (preferir
        # H.264 combinado antes que un DASH VP9, igual que en download_video) y los de
        # imagen caen al único formato disponible (la foto).
        "format": (
            f"bestvideo[vcodec^=avc][height<={h}]+bestaudio[ext=m4a]"
            f"/best[height<={h}][ext=mp4]/best[ext=mp4]"
            f"/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        ),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        # Los items de foto no tienen formato de video: sin esto yt-dlp lanza
        # "No video formats found!" y aborta el post entero. Las fotos se bajan aparte.
        "ignore_no_formats_error": True,
        "max_filesize": MAX_DOCUMENT_SIZE_BYTES,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [_progress_hook],
        "http_headers": {"User-Agent": _UA},
    }

    def _do_download() -> list[dict]:
        items: list[dict] = []
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
                    path = _ensure_h264(path)
                items.append({"path": path, "kind": kind})
        return items

    return _run_with_retry(_do_download)


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


def get_video_info(url: str, max_height: int | None = None) -> dict:
    """
    Obtiene metadatos del video sin descargarlo.
    Retorna title, duration (segundos) y filesize (bytes, puede ser None).
    Lanza yt_dlp.DownloadError si el video no existe o es privado.
    """
    h = max_height or MAX_VIDEO_HEIGHT
    _base_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": (
            f"bestvideo[vcodec^=avc][height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        ),
        "format_sort": ["res", "fps", "vcodec:avc", "ext:mp4", "acodec:m4a"],
        # Necesario para posts de foto (single o carrusel): sin esto el preflight
        # revienta con "No video formats found!" antes de poder clasificarlos.
        "ignore_no_formats_error": True,
        "http_headers": {"User-Agent": _UA},
    }
    def _extract() -> dict:
        with yt_dlp.YoutubeDL(_base_opts) as ydl:
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
            }
        # Un único item envuelto en "playlist": tratarlo como item suelto para poder
        # clasificarlo (foto vs video) y enrutarlo bien.
        info = entries[0]

    return {
        "title": info.get("title") or "Sin título",
        "duration": info.get("duration"),
        "filesize": _estimate_filesize(info),
        "is_music": bool(info.get("track") or info.get("artist")),
        "is_playlist": False,
        "is_image": _is_image_entry(info),
        "count": 1,
        "song": _identify_song(info),
    }


def download_audio(url: str, on_progress: Callable[[str], None] | None = None) -> tuple[str, dict]:
    """
    Descarga solo el audio en MP3.
    Devuelve (ruta_mp3, {"title": str, "artist": str | None}).
    Lanza yt_dlp.DownloadError si algo falla.
    """
    output_template = _make_output_path()

    def _progress_hook(d: dict) -> None:
        if on_progress:
            on_progress(d.get("status", ""))

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
        "extractor_args": {"youtube": {"skip": ["dash", "hls"]}},
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_DOCUMENT_SIZE_BYTES,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [_progress_hook],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

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
    output_template = _make_output_path()

    def _progress_hook(d: dict) -> None:
        if on_progress:
            on_progress(d.get("status", ""))

    ydl_opts = {
        "outtmpl": output_template,
        "format": "bestaudio/best",
        "noplaylist": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_DOCUMENT_SIZE_BYTES,
        "retries": 3,
        "fragment_retries": 3,
        "progress_hooks": [_progress_hook],
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }

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
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }
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
    cmd = ["ffmpeg", "-y", "-i", filepath]
    if max_height:
        # scale=-2:min(h,ih) → baja a max_height solo si el original es más alto
        # (-2 mantiene el ancho par y la relación de aspecto). No hace upscale.
        cmd += ["-vf", f"scale=-2:'min({max_height},ih)'"]
    cmd += [
        "-c:v", "libx264", "-b:v", str(video_bps),
        "-maxrate", str(int(video_bps * 1.5)),
        "-bufsize", str(int(video_bps * 2)),
        "-preset", "veryfast",
        # Fuerza 8-bit 4:2:0: si el origen es 10-bit/4:4:4, sin esto libx264 saca un
        # perfil (High 10/4:4:4) que Telegram muestra negro/congelado.
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        out,
    ]

    try:
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
