import json
import os
import subprocess
import uuid
from collections.abc import Callable
import yt_dlp
from config import DOWNLOAD_DIR, MAX_DOCUMENT_SIZE_BYTES, MAX_VIDEO_HEIGHT, MAX_PREFLIGHT_SIZE_BYTES


def _make_output_path() -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    return os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.%(ext)s")


def download_video(url: str, on_progress: Callable[[str], None] | None = None) -> str:
    """
    Descarga el video de la URL dada y devuelve la ruta al archivo.
    Lanza yt_dlp.DownloadError si algo falla.
    on_progress recibe el status string de yt-dlp ("downloading", "finished", etc).
    """
    output_template = _make_output_path()

    def _progress_hook(d: dict) -> None:
        if on_progress:
            on_progress(d.get("status", ""))

    ydl_opts = {
        "outtmpl": output_template,
        # Prioriza H.264 (avc) para máxima compatibilidad; VP9 requiere re-encode
        "format": (
            f"bestvideo[vcodec^=avc][height<={MAX_VIDEO_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[vcodec^=avc][height<={MAX_VIDEO_HEIGHT}]+bestaudio"
            f"/bestvideo[height<={MAX_VIDEO_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]"
            f"/best[height<={MAX_VIDEO_HEIGHT}][ext=mp4]/best[height<={MAX_VIDEO_HEIGHT}]/best"
        ),
        "merge_output_format": "mp4",
        "postprocessor_args": {
            "merger": ["-c:v", "libx264", "-c:a", "aac", "-crf", "23"],
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

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if not os.path.exists(filename):
            filename = filename.rsplit(".", 1)[0] + ".mp4"

    return filename


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


def get_video_info(url: str) -> dict:
    """
    Obtiene metadatos del video sin descargarlo.
    Retorna title, duration (segundos) y filesize (bytes, puede ser None).
    Lanza yt_dlp.DownloadError si el video no existe o es privado.
    """
    _base_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": (
            f"bestvideo[vcodec^=avc][height<={MAX_VIDEO_HEIGHT}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={MAX_VIDEO_HEIGHT}]+bestaudio/best[height<={MAX_VIDEO_HEIGHT}]/best"
        ),
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
    }
    with yt_dlp.YoutubeDL(_base_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    return {
        "title": info.get("title") or "Sin título",
        "duration": info.get("duration"),
        "filesize": _estimate_filesize(info),
        "is_music": bool(info.get("track") or info.get("artist")),
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
            "preferredquality": "192",
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

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        filename = filename.rsplit(".", 1)[0] + ".mp3"

    title = info.get("track") or info.get("title") or "Sin título"
    artist = info.get("artist") or info.get("creator") or None
    return filename, {"title": title, "artist": artist}


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
