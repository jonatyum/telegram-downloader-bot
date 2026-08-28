"""
API HTTP para el canal web: recibe un link, ejecuta la descarga contra pipeline.py y
sirve el resultado como archivo descargable. Corre como un servicio de Render
**separado** del bot de Telegram — no importa nada de bot.py ni de telegram, así que no
necesita BOT_TOKEN ni toca el proceso del bot en absoluto.

A diferencia de Telegram, aquí no hay tope de 50 MB ni límite de edición de mensajes:
el progreso se manda por SSE con porcentaje real, y el archivo se sirve por streaming
desde disco en vez de cargarlo entero en memoria.
"""
import asyncio
import base64
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from config import DOWNLOAD_DIR, MAX_COMPRESS_HEIGHT, MAX_CONCURRENT_DOWNLOADS, MAX_VIDEO_HEIGHT
from downloader import fetch_thumbnail, get_video_info
from links import is_supported_url
from pipeline import DeliveryLimits, Pipeline, download_error_message
from rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Origen(es) desde donde se sirve el frontend (GitHub Pages). "*" por defecto: es una
# API pública de solo lectura (GET/POST sin cookies), así que un origin abierto no
# expone nada que un atacante no pudiera pedir igual con curl.
WEB_ORIGINS = [o.strip() for o in os.getenv("WEB_ORIGINS", "*").split(",") if o.strip()]

# Cuánto vive un resultado en disco antes de que el barrido lo borre. Es la única red de
# seguridad de espacio: si nadie descarga el archivo, no debe quedarse para siempre.
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_MINUTES", "30")) * 60
_SWEEP_INTERVAL_SECONDS = 60

# Sin límite de tamaño real: no hay Bot API de por medio. 2 GB es solo una red de
# seguridad de disco (Render free tiene poco espacio).
_WEB_MAX_INLINE_BYTES = int(os.getenv("MAX_WEB_FILE_MB", "2000")) * 1024 * 1024
DELIVERY_LIMITS = DeliveryLimits(max_inline_bytes=_WEB_MAX_INLINE_BYTES, max_compress_height=MAX_COMPRESS_HEIGHT)

# Los resultados se guardan fuera de DOWNLOAD_DIR: pipeline.py borra todo lo que quede
# en DOWNLOAD_DIR en su `finally`, así que un archivo listo para descargar tiene que
# vivir en otro sitio o el pipeline lo borraría antes de que el usuario lo pida.
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")

_pipeline = Pipeline(MAX_CONCURRENT_DOWNLOADS, MAX_VIDEO_HEIGHT)

# Máximo de solicitudes de creación de job por IP en la ventana del rate limiter
# (comparte RATE_LIMIT_WINDOW con el bot, propio en número de requests). RateLimiter no
# impone que la clave sea un int: un string de IP funciona igual como clave de diccionario.
_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("WEB_RATE_LIMIT_REQUESTS", "8"))
_limiter = RateLimiter(max_requests=_RATE_LIMIT_MAX_REQUESTS)


@dataclass
class Job:
    id: str
    status: str = "queued"  # queued | running | ready | error
    phase_text: str = "En cola..."
    error: str | None = None
    result_path: str | None = None
    result_items: list[dict] | None = None  # carrusel: varios archivos
    download_name: str | None = None
    created_at: float = field(default_factory=time.time)
    _subscribers: list[asyncio.Queue] = field(default_factory=list)

    async def publish(self, event: dict) -> None:
        for q in list(self._subscribers):
            await q.put(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q


_jobs: dict[str, Job] = {}


class WebMessenger:
    """
    Adaptador de pipeline.Messenger para el canal web. "Enviar" no empuja bytes a
    ningún sitio: mueve el archivo resultante fuera de DOWNLOAD_DIR (para que el
    `finally` del pipeline no lo borre) y lo deja disponible para que el navegador lo
    pida por GET /jobs/{id}/file. El progreso se publica a la cola SSE del job.
    """

    def __init__(self, job: Job, limits: DeliveryLimits):
        self._job = job
        self.limits = limits

    def _claim(self, path: str) -> str:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        ext = path.rsplit(".", 1)[-1]
        dest = os.path.join(RESULTS_DIR, f"{self._job.id}.{ext}")
        shutil.move(path, dest)
        return dest

    async def update(self, text: str) -> None:
        self._job.phase_text = text
        await self._job.publish({"type": "status", "text": text})

    async def finish(self) -> None:
        self._job.status = "ready"
        await self._job.publish({"type": "ready"})

    async def send_video(self, path, *, width, height, caption, song):
        self._job.result_path = self._claim(path)

    async def send_audio(self, path, *, title, performer, filename):
        self._job.result_path = self._claim(path)
        self._job.download_name = filename

    async def send_photo(self, path):
        self._job.result_path = self._claim(path)

    async def send_document(self, path, *, caption, song):
        self._job.result_path = self._claim(path)

    async def send_album(self, items: list[dict]) -> None:
        self._job.result_items = [
            {"path": self._claim(it["path"]), "kind": it["kind"]} for it in items
        ]


def _job_files(job: Job) -> list[str]:
    if job.result_items is not None:
        return [it["path"] for it in job.result_items]
    return [job.result_path] if job.result_path else []


def _cleanup_job(job: Job) -> None:
    for path in _job_files(job):
        if path and os.path.exists(path):
            os.remove(path)


async def _sweep_loop() -> None:
    """Borra jobs (y sus archivos) más viejos que JOB_TTL_SECONDS. Es la única forma de
    liberar el disco de los resultados que nadie descargó."""
    while True:
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
        cutoff = time.time() - JOB_TTL_SECONDS
        expired = [jid for jid, j in _jobs.items() if j.created_at < cutoff]
        for jid in expired:
            job = _jobs.pop(jid, None)
            if job:
                _cleanup_job(job)
        if expired:
            logger.info("Barrido: %d job(s) expirado(s) eliminados", len(expired))


async def _run_job(job: Job, url: str, kind: str, resolution: int | None) -> None:
    job.status = "running"
    messenger = WebMessenger(job, DELIVERY_LIMITS)
    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, get_video_info, url, resolution or MAX_VIDEO_HEIGHT)

        if (info.get("is_playlist") and info.get("count", 1) > 1) or info.get("is_image"):
            await _pipeline.carousel(url, messenger=messenger, user_pref_height=resolution)
        else:
            await _pipeline.download(
                url, kind, messenger=messenger, user_pref_height=resolution, song=info.get("song"),
            )
    except yt_dlp.DownloadError as e:
        logger.warning("DownloadError para %s: %s", url, e)
        job.status = "error"
        job.error = download_error_message(str(e))
        await job.publish({"type": "error", "text": job.error})
    except Exception:
        logger.exception("Error inesperado en job %s (%s)", job.id, url)
        job.status = "error"
        job.error = "Ocurrió un error inesperado. Inténtalo de nuevo."
        await job.publish({"type": "error", "text": job.error})


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _sniff_image_mime(data: bytes) -> str:
    """
    Adivina el content-type del thumbnail por sus primeros bytes (magic numbers) en vez
    de confiar en la extensión de la URL, que casi nunca refleja el formato real.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"  # la inmensa mayoría de thumbnails de video son JPEG


class PreviewBody(BaseModel):
    url: str


class CreateJobBody(BaseModel):
    url: str
    kind: str = "video"
    resolution: int | None = None

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        if v not in ("video", "audio"):
            raise ValueError("kind debe ser 'video' o 'audio'")
        return v

    @field_validator("resolution")
    @classmethod
    def _valid_resolution(cls, v: int | None) -> int | None:
        if v is not None and v not in (360, 480, 720, 1080):
            raise ValueError("resolution debe ser 360, 480, 720 o 1080")
        return v


async def _lifespan(app: FastAPI):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    sweep_task = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        sweep_task.cancel()


app = FastAPI(title="Downloader API", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=WEB_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


def _client_ip(request: Request) -> str:
    # Render pone al servicio detrás de un proxy: la IP real viene en X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/preview")
async def preview(body: PreviewBody, request: Request):
    """
    Verifica el link y devuelve título/miniatura sin descargar nada — lo que el
    frontend muestra antes de dejar tocar "Descargar". Comparte el rate limiter con
    /jobs: sigue siendo tráfico contra la misma plataforma externa.
    """
    ip = _client_ip(request)
    if not _limiter.is_allowed(ip):
        wait = _limiter.seconds_until_reset(ip)
        raise HTTPException(429, f"Demasiadas solicitudes. Espera {wait} segundos.")

    if not is_supported_url(body.url):
        raise HTTPException(400, "Enlace no soportado. Prueba con TikTok, Instagram, Facebook, YouTube o X/Twitter.")

    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, get_video_info, body.url, MAX_VIDEO_HEIGHT)
    except yt_dlp.DownloadError as e:
        logger.warning("DownloadError en preview para %s: %s", body.url, e)
        raise HTTPException(422, download_error_message(str(e)))

    thumbnail = None
    thumb_url = info.get("thumbnail")
    if thumb_url:
        # Se descarga en el servidor (con el User-Agent correcto) y se embebe como
        # data URI: muchas plataformas bloquean el hotlinking directo desde <img src>,
        # así que servirlo desde nuestro propio origen es lo único que funciona siempre.
        raw = await loop.run_in_executor(None, fetch_thumbnail, thumb_url)
        if raw:
            mime = _sniff_image_mime(raw)
            thumbnail = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    return {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": thumbnail,
        "is_playlist": bool(info.get("is_playlist")),
        "count": info.get("count", 1),
    }


@app.post("/jobs", status_code=202)
async def create_job(body: CreateJobBody, request: Request):
    ip = _client_ip(request)
    if not _limiter.is_allowed(ip):
        wait = _limiter.seconds_until_reset(ip)
        raise HTTPException(429, f"Demasiadas solicitudes. Espera {wait} segundos.")

    if not is_supported_url(body.url):
        raise HTTPException(400, "Enlace no soportado. Prueba con TikTok, Instagram, Facebook, YouTube o X/Twitter.")

    job = Job(id=uuid.uuid4().hex)
    _jobs[job.id] = job
    asyncio.create_task(_run_job(job, body.url, body.kind, body.resolution))
    return {"job_id": job.id}


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado.")

    async def gen():
        yield _sse({"type": "status", "text": job.phase_text})
        if job.status == "ready":
            yield _sse({"type": "ready"})
            return
        if job.status == "error":
            yield _sse({"type": "error", "text": job.error})
            return

        queue = job.subscribe()
        while True:
            event = await queue.get()
            yield _sse(event)
            if event["type"] in ("ready", "error"):
                return

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/jobs/{job_id}/file")
async def job_file(job_id: str, index: int = 0):
    job = _jobs.get(job_id)
    if not job or job.status != "ready":
        raise HTTPException(404, "El archivo todavía no está listo.")

    files = _job_files(job)
    if index >= len(files):
        raise HTTPException(404, "Índice fuera de rango.")

    path = files[index]
    if not path or not os.path.exists(path):
        raise HTTPException(410, "El archivo ya no está disponible (expiró).")

    filename = job.download_name or os.path.basename(path)
    return FileResponse(path, filename=filename)


@app.get("/jobs/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado.")
    return {
        "status": job.status,
        "phase": job.phase_text,
        "error": job.error,
        "files": len(_job_files(job)) if job.status == "ready" else 0,
    }


@app.delete("/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    job = _jobs.pop(job_id, None)
    if job:
        _cleanup_job(job)


@app.get("/health")
async def health():
    return {"status": "ok"}
