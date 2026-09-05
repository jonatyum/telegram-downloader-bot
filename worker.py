"""
Worker remoto de YouTube: el mismo motor de descarga, corriendo en una máquina con
conexión residencial y publicado por un túnel (Cloudflare Tunnel, Tailscale Funnel...).

Existe por una sola razón: YouTube bloquea a las IPs de datacenter con su chequeo
antibot, y el bloqueo no se puede esquivar con otra librería ni con otro cliente porque
ocurre antes de mirar quién pregunta. Lo único que lo cambia es de dónde sale la
petición. Y como YouTube firma las URLs de los formatos con la IP que las pidió
(el parámetro `ip` va dentro de `sparams`), no alcanza con resolver el link acá y bajar
los bytes en el servidor: la descarga entera tiene que pasar por esta conexión. Por eso
este worker devuelve el archivo terminado y no una lista de URLs.

Corre las mismas funciones de `downloader.py` que correría el servidor, así que no
duplica lógica de descarga: es una cáscara HTTP alrededor del módulo que ya existe.

    uvicorn worker:app --host 127.0.0.1 --port 8100

Del otro lado, el servicio de Render lo usa poniendo YOUTUBE_WORKER_URL y
YOUTUBE_WORKER_TOKEN. Si este worker no responde, el servidor sigue por su camino
local: apagar esta máquina degrada YouTube, no rompe la aplicación.
"""
import asyncio
import json
import logging
import os
import secrets

import yt_dlp
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from config import MAX_VIDEO_HEIGHT, YOUTUBE_WORKER_TOKEN
from downloader import (
    download_audio,
    download_song,
    download_video,
    get_audio_info,
    get_video_info,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="YouTube worker")


def _authorize(token: str | None) -> None:
    """
    El túnel deja esto expuesto a internet: sin token cualquiera podría usar tu
    conexión de casa para descargar. `compare_digest` evita filtrar el token por
    diferencia de tiempos al comparar.
    """
    if not YOUTUBE_WORKER_TOKEN:
        raise HTTPException(503, "El worker no tiene YOUTUBE_WORKER_TOKEN configurado.")
    if not token or not secrets.compare_digest(token, YOUTUBE_WORKER_TOKEN):
        raise HTTPException(401, "Token inválido.")


class UrlBody(BaseModel):
    url: str
    max_height: int | None = None


class QueryBody(BaseModel):
    query: str


async def _run(fn, *args):
    """
    Igual que en el resto del proyecto: `downloader` es síncrono a propósito y se
    llama en el thread pool para no bloquear el loop.
    """
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, fn, *args)
    except yt_dlp.DownloadError as e:
        # 422 es el código que el cliente interpreta como "el worker anduvo, yt-dlp
        # falló": lo propaga tal cual en vez de reintentar en local, para no cambiar
        # "video privado" por el chequeo antibot del servidor.
        raise HTTPException(422, str(e)) from e


def _file_response(path: str, meta: dict | None = None) -> FileResponse:
    """
    Devuelve el archivo y lo borra en cuanto termina de enviarse: esta máquina es un
    intermediario, no un almacén. Los metadatos viajan en cabecera porque el cuerpo
    ya está ocupado por el archivo.
    """
    headers = {"X-Filename": os.path.basename(path)}
    if meta is not None:
        headers["X-Meta"] = _json_header(meta)
    return FileResponse(
        path,
        filename=os.path.basename(path),
        headers=headers,
        background=BackgroundTask(_cleanup, path),
    )


def _json_header(meta: dict) -> str:
    """Cabecera HTTP: solo latin-1 y sin saltos de línea, así que se escapa a ASCII."""
    return json.dumps(meta, ensure_ascii=True)


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        logger.warning("No pude borrar el temporal %s", os.path.basename(path))


@app.post("/info")
async def info(body: UrlBody, x_worker_token: str | None = Header(default=None)):
    _authorize(x_worker_token)
    return await _run(get_video_info, body.url, body.max_height or MAX_VIDEO_HEIGHT)


@app.post("/audio-info")
async def audio_info(body: UrlBody, x_worker_token: str | None = Header(default=None)):
    _authorize(x_worker_token)
    return await _run(get_audio_info, body.url)


@app.post("/video")
async def video(body: UrlBody, x_worker_token: str | None = Header(default=None)):
    _authorize(x_worker_token)
    path = await _run(download_video, body.url, None, body.max_height)
    logger.info("Video entregado (%.1f MB)", os.path.getsize(path) / 1024 / 1024)
    return _file_response(path)


@app.post("/audio")
async def audio(body: UrlBody, x_worker_token: str | None = Header(default=None)):
    _authorize(x_worker_token)
    path, meta = await _run(download_audio, body.url, None)
    logger.info("Audio entregado (%.1f MB)", os.path.getsize(path) / 1024 / 1024)
    return _file_response(path, meta)


@app.post("/song")
async def song(body: QueryBody, x_worker_token: str | None = Header(default=None)):
    _authorize(x_worker_token)
    path, meta = await _run(download_song, body.query, None)
    logger.info("Canción entregada (%.1f MB)", os.path.getsize(path) / 1024 / 1024)
    return _file_response(path, meta)


@app.get("/health")
async def health():
    """Sin token: solo dice que el proceso está vivo, no revela nada."""
    return {"status": "ok"}
