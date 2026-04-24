import json
import os
import subprocess
import uuid
from collections.abc import Callable
import yt_dlp
from config import DOWNLOAD_DIR, MAX_DOCUMENT_SIZE_BYTES


def _make_output_path() -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    return os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4()}.%(ext)s")


def download_video(url: str, on_progress: Callable[[dict], None] | None = None) -> str:
    """
    Descarga el video de la URL dada y devuelve la ruta al archivo.
    Lanza yt_dlp.DownloadError si algo falla.
    on_progress recibe el dict de progreso de yt-dlp en cada actualización.
    """
    output_template = _make_output_path()

    def _progress_hook(d: dict) -> None:
        if on_progress:
            on_progress(d.get("status", ""))

    ydl_opts = {
        "outtmpl": output_template,
        # Prioriza H.264 (avc) para máxima compatibilidad; VP9 requiere re-encode
        "format": (
            "bestvideo[vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[vcodec^=avc]+bestaudio"
            "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/best[ext=mp4]/best"
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
