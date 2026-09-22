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
import time
import uuid

import yt_dlp
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
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
    # Lo manda un servidor nuevo; uno viejo no lo manda y se queda en False, que es
    # exactamente el comportamiento de siempre.
    best_quality: bool = False


class QueryBody(BaseModel):
    query: str


async def _run(fn, *args):
    """
    Igual que en el resto del proyecto: `downloader` es síncrono a propósito y se
    llama en el thread pool para no bloquear el loop.
    """
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    # getattr con respaldo: en los tests `fn` es un mock, y los mocks no tienen __name__.
    nombre = getattr(fn, "__name__", type(fn).__name__)
    logger.info("→ %s: empieza", nombre)
    try:
        result = await loop.run_in_executor(None, fn, *args)
    except yt_dlp.DownloadError as e:
        # Este log es el que dice si YouTube bloquea la conexión DEL WORKER. Si aparece,
        # el problema no es el transporte y no lo arregla tocar el túnel.
        logger.warning("✗ %s: yt-dlp falló tras %.1fs — %s", nombre, time.monotonic() - started, e)
        # 422 es el código que el cliente interpreta como "el worker anduvo, yt-dlp
        # falló": lo propaga tal cual en vez de reintentar en local, para no cambiar
        # "video privado" por el chequeo antibot del servidor.
        raise HTTPException(422, str(e)) from e
    logger.info("✓ %s: terminó en %.1fs", nombre, time.monotonic() - started)
    return result


# ---------------------------------------------------------------------------
# Trabajos en curso
#
# Las descargas NO se sirven en la misma petición que las pide. El motivo es medido,
# no teórico: descargar el archivo entero antes de responder hace que el tiempo hasta
# la primera cabecera sea la duración completa de la descarga, y los túneles que
# publican este worker cancelan la petición mucho antes (Cloudflare corta pasados unos
# ~100 s). Un video de 732 MB tardó 147 s y el túnel lo canceló a los 124 s: el worker
# terminó su trabajo y se puso a enviar por un socket que ya nadie escuchaba.
#
# Con este esquema ninguna petición dura más que un intercambio corto: se acepta el
# trabajo y se responde al instante, el servidor pregunta por el estado, y cuando el
# archivo existe lo recoge — ahí las cabeceras salen de inmediato porque no hay nada
# que esperar. El límite del túnel es sobre el tiempo hasta la primera cabecera, no
# sobre lo que dure la transferencia, así que un archivo grande ya pasa sin problema.
_jobs: dict[str, dict] = {}
_JOB_TTL_SECONDS = 30 * 60


def _sweep_jobs() -> None:
    """Descarta trabajos que nadie recogió: esta máquina es un intermediario, no un almacén."""
    ahora = time.monotonic()
    for jid, job in list(_jobs.items()):
        if ahora - job["created"] > _JOB_TTL_SECONDS:
            if job.get("path"):
                _cleanup(job["path"])
            _jobs.pop(jid, None)
            logger.info("Job %s descartado por antigüedad.", jid[:8])


def _speaks_jobs(header: str | None) -> bool:
    """
    El servidor anuncia con X-Worker-Protocol que entiende el flujo de trabajos.

    Sin esa cabecera se responde como antes, con el archivo en la propia respuesta. No
    es cortesía: un servidor viejo no mira el código de estado y trata el cuerpo como el
    archivo, así que recibiría el JSON del id —unas decenas de bytes— y lo guardaría
    como si fuera el video. Pasó de verdad al reiniciar el worker antes de desplegar el
    servidor, y por eso la negociación es explícita en lugar de asumir la versión.
    """
    try:
        return int(header or 0) >= 2
    except ValueError:
        return False


async def _start_job(fn, *args) -> dict:
    """Acepta el trabajo, lo lanza en segundo plano y devuelve su id de inmediato."""
    _sweep_jobs()
    jid = uuid.uuid4().hex
    _jobs[jid] = {"status": "running", "path": None, "meta": None,
                  "error": None, "created": time.monotonic()}
    asyncio.create_task(_execute_job(jid, fn, *args))
    return {"job": jid}


async def _execute_job(jid: str, fn, *args) -> None:
    try:
        result = await _run(fn, *args)
    except HTTPException as e:
        # _run ya convirtió el DownloadError en 422; acá se guarda para que el servidor
        # lo reciba al preguntar por el estado y lo trate igual que antes.
        _jobs[jid].update(status="error", error=str(e.detail))
        return
    except Exception as e:
        logger.exception("Job %s falló de forma inesperada.", jid[:8])
        _jobs[jid].update(status="error", error=f"El worker falló: {e}")
        return
    # download_video devuelve una ruta; download_audio y download_song, (ruta, metadatos).
    path, meta = result if isinstance(result, tuple) else (result, None)
    _jobs[jid].update(status="ready", path=path, meta=meta)
    logger.info("Job %s listo: %.1f MB esperando recogida.",
                jid[:8], os.path.getsize(path) / 1024 / 1024)


def _file_response(path: str, meta: dict | None = None) -> FileResponse:
    """
    Devuelve el archivo y lo borra en cuanto termina de enviarse: esta máquina es un
    intermediario, no un almacén. Los metadatos viajan en cabecera porque el cuerpo
    ya está ocupado por el archivo.
    """
    headers = {"X-Filename": os.path.basename(path)}
    if meta is not None:
        headers["X-Meta"] = _json_header(meta)
    # Si esta línea sale y del otro lado nunca llega el archivo, la descarga terminó
    # bien y lo que falló fue el transporte (túnel o subida), no el worker.
    logger.info("↑ Enviando %.1f MB al servidor", os.path.getsize(path) / 1024 / 1024)
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
    return await _run(get_video_info, body.url, body.max_height or MAX_VIDEO_HEIGHT, body.best_quality)


@app.post("/audio-info")
async def audio_info(body: UrlBody, x_worker_token: str | None = Header(default=None)):
    _authorize(x_worker_token)
    return await _run(get_audio_info, body.url)


@app.post("/video")
async def video(body: UrlBody, x_worker_token: str | None = Header(default=None),
                x_worker_protocol: str | None = Header(default=None)):
    _authorize(x_worker_token)
    if _speaks_jobs(x_worker_protocol):
        return JSONResponse(
            await _start_job(download_video, body.url, None, body.max_height, body.best_quality),
            status_code=202)
    return _file_response(await _run(download_video, body.url, None, body.max_height, body.best_quality))


@app.post("/audio")
async def audio(body: UrlBody, x_worker_token: str | None = Header(default=None),
                x_worker_protocol: str | None = Header(default=None)):
    _authorize(x_worker_token)
    if _speaks_jobs(x_worker_protocol):
        return JSONResponse(await _start_job(download_audio, body.url, None), status_code=202)
    path, meta = await _run(download_audio, body.url, None)
    return _file_response(path, meta)


@app.post("/song")
async def song(body: QueryBody, x_worker_token: str | None = Header(default=None),
               x_worker_protocol: str | None = Header(default=None)):
    _authorize(x_worker_token)
    if _speaks_jobs(x_worker_protocol):
        return JSONResponse(await _start_job(download_song, body.query, None), status_code=202)
    path, meta = await _run(download_song, body.query, None)
    return _file_response(path, meta)


@app.get("/jobs/{job_id}")
async def job_status(job_id: str, x_worker_token: str | None = Header(default=None)):
    _authorize(x_worker_token)
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Ese trabajo no existe o ya expiró.")
    return {"status": job["status"], "error": job["error"]}


@app.get("/jobs/{job_id}/file")
async def job_file(job_id: str, x_worker_token: str | None = Header(default=None)):
    _authorize(x_worker_token)
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Ese trabajo no existe o ya expiró.")
    if job["status"] != "ready":
        estado = job["status"]
        raise HTTPException(409, f"El trabajo todavía está en estado '{estado}'.")
    _jobs.pop(job_id, None)  # un archivo se recoge una sola vez
    return _file_response(job["path"], job["meta"])


@app.get("/health")
async def health():
    """Sin token: solo dice que el proceso está vivo, no revela nada."""
    return {"status": "ok"}
