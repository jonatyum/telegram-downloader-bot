"""
Orquesta una descarga ya decidida (formato, url, preferencias): llama al motor de
downloader.py, decide cómo entregar el resultado dentro de los límites del canal y
limpia los archivos temporales. No importa nada de telegram — un canal nuevo (web,
WhatsApp...) solo necesita implementar Messenger.

La decisión de QUÉ descargar (elegir formato, mostrar teclados, rutear YouTube) sigue
viviendo en cada canal: es presentación, no orquestación, y cada canal la resuelve muy
distinto (botones inline en Telegram, un formulario en la web).
"""
import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from downloader import (
    _IMAGE_EXTS,
    compress_video,
    download_audio,
    download_post,
    download_song,
    download_video,
    get_video_dimensions,
)

logger = logging.getLogger(__name__)

# Telegram permite hasta 10 elementos por álbum (media group). Es el único canal hoy;
# un canal nuevo con otro límite lo pasaría por su propio Messenger.send_album.
MEDIA_GROUP_MAX = 10


@dataclass(frozen=True, slots=True)
class DeliveryLimits:
    """Los topes que le importan al pipeline al decidir cómo entregar un video."""
    max_inline_bytes: int          # por encima, se intenta comprimir o cae a documento
    max_compress_height: int | None


@runtime_checkable
class Messenger(Protocol):
    """
    Lo que un canal tiene que ofrecer para que el pipeline pueda reportar progreso y
    entregar un resultado. Ningún método sabe de archivos temporales: reciben una ruta
    ya lista en disco y hacen lo que su plataforma necesite con ella.
    """
    limits: DeliveryLimits

    async def update(self, text: str) -> None: ...
    async def finish(self) -> None: ...
    async def send_video(self, path: str, *, width: int | None, height: int | None,
                          caption: str | None, song: dict | None) -> None: ...
    async def send_audio(self, path: str, *, title: str, performer: str | None,
                          filename: str) -> None: ...
    async def send_photo(self, path: str) -> None: ...
    async def send_document(self, path: str, *, caption: str | None, song: dict | None) -> None: ...
    async def send_album(self, items: list[dict]) -> None: ...


_PHASE_MESSAGES = {
    "downloading": "⬇️ Descargando",
    "finished":    "🔄 Procesando",
}


def _progress_bridge(loop: asyncio.AbstractEventLoop, messenger: Messenger) -> Callable[[str], None]:
    """
    Traduce el status síncrono de yt-dlp (el hook corre en el hilo del executor) a un
    update() async del canal. Solo reporta cambios de fase: editar un mensaje de Telegram
    más de una vez por segundo lo bloquea. El try/except es specific de esta vía
    fire-and-forget (run_coroutine_threadsafe) — un fallo aquí no debe perderse como
    "exception never retrieved" ni tampoco debe propagar y tumbar el hilo del hook.
    """
    last_phase = [None]

    async def _report(text: str) -> None:
        try:
            await messenger.update(text)
        except Exception:
            pass

    def callback(status: str) -> None:
        text = _PHASE_MESSAGES.get(status)
        if text is None or status == last_phase[0]:
            return
        last_phase[0] = status
        asyncio.run_coroutine_threadsafe(_report(text), loop)

    return callback


def _audio_filename(title: str, artist: str | None) -> str:
    return f"{artist} - {title}.mp3" if artist else f"{title}.mp3"


def _quality_note(user_pref: int | None, height: int) -> str | None:
    if not user_pref or not height:
        return None
    if height < user_pref:
        return f"📐 Solo estaba disponible en {height}p (pediste {user_pref}p)"
    if height > user_pref:
        return f"📐 Bajado en {height}p: no había nada de {user_pref}p o menos"
    return None


def download_error_message(reason: str) -> str:
    """Traduce un DownloadError de yt-dlp a un mensaje que un usuario pueda entender."""
    reason = reason.lower()
    if "private" in reason or "login" in reason:
        return "🔒 Ese post es privado o pide iniciar sesión. Solo puedo con contenido público."
    if "not found" in reason or "404" in reason:
        return "🔍 No encontré nada en ese link. Revisa que esté completo."
    if "unable to extract" in reason or "rehydration" in reason:
        return "🛠️ La plataforma cambió algo y no puedo leerlo ahora. Prueba de nuevo en un rato."
    return "⚠️ No pude descargar eso. Puede ser privado, borrado, o el link ya venció."


class Pipeline:
    """
    Ejecuta una descarga contra el motor y entrega el resultado a través de un Messenger.
    El límite de descargas simultáneas vive aquí (no en cada canal) porque protege al
    host — CPU/RAM — y ese cupo se comparte entre todos los canales por igual.
    """

    def __init__(self, max_concurrent_downloads: int, default_max_height: int):
        self._semaphore = asyncio.Semaphore(max_concurrent_downloads)
        self._default_height = default_max_height

    async def download(
        self,
        url: str,
        fmt: str,
        *,
        messenger: Messenger,
        user_pref_height: int | None = None,
        song: dict | None = None,
    ) -> None:
        await messenger.update("⏳ En cola")
        filepath = None
        extra_path = None  # archivo comprimido temporal, si se genera
        effective_height = user_pref_height or self._default_height

        async with self._semaphore:
            await messenger.update("⬇️ Descargando...")
            loop = asyncio.get_running_loop()
            progress_cb = _progress_bridge(loop, messenger)

            try:
                if fmt == "audio":
                    filepath, meta = await loop.run_in_executor(None, download_audio, url, progress_cb)
                    title = meta["title"]
                    artist = meta.get("artist")
                    await messenger.update("📤 Preparando el archivo")
                    await messenger.send_audio(
                        filepath, title=title, performer=artist,
                        filename=_audio_filename(title, artist),
                    )
                else:
                    filepath = await loop.run_in_executor(None, download_video, url, progress_cb, effective_height)

                    # Post de una sola foto (link de imagen, no video): enviar como foto.
                    if filepath.rsplit(".", 1)[-1].lower() in _IMAGE_EXTS:
                        await messenger.update("📤 Preparando el archivo")
                        await messenger.send_photo(filepath)
                        await messenger.finish()
                        return

                    file_size = os.path.getsize(filepath)
                    width, height = get_video_dimensions(filepath)
                    quality_note = _quality_note(user_pref_height, height)

                    send_path = filepath
                    as_document = False

                    if file_size > messenger.limits.max_inline_bytes:
                        # Intenta comprimir para poder enviarlo como video reproducible.
                        await messenger.update("🗜️ El video es grande, comprimiendo")
                        compressed = await loop.run_in_executor(
                            None, compress_video, filepath,
                            messenger.limits.max_inline_bytes, messenger.limits.max_compress_height,
                        )
                        if compressed:
                            extra_path = compressed
                            send_path = compressed
                            width, height = get_video_dimensions(send_path)
                        else:
                            as_document = True  # la compresión no bastó: cae a documento

                    if as_document:
                        await messenger.update("📦 No pude comprimirlo, va como archivo")
                        await messenger.send_document(filepath, caption=quality_note, song=song)
                    else:
                        await messenger.update("📤 Preparando el archivo")
                        await messenger.send_video(
                            send_path, width=width or None, height=height or None,
                            caption=quality_note, song=song,
                        )

                await messenger.finish()

            finally:
                for path in (filepath, extra_path):
                    if path and os.path.exists(path):
                        os.remove(path)

    async def carousel(
        self,
        url: str,
        *,
        messenger: Messenger,
        user_pref_height: int | None = None,
    ) -> None:
        await messenger.update("⏳ En cola")
        items: list[dict] = []
        effective_height = user_pref_height or self._default_height

        async with self._semaphore:
            await messenger.update("⬇️ Descargando...")
            loop = asyncio.get_running_loop()
            progress_cb = _progress_bridge(loop, messenger)

            try:
                items = await loop.run_in_executor(None, download_post, url, progress_cb, effective_height)
                if not items:
                    await messenger.update("⚠️ No pude descargar el contenido de ese post.")
                    return

                await messenger.update(f"📤 Preparando {len(items)} elementos")
                await messenger.send_album(items)
                await messenger.finish()

            finally:
                for it in items:
                    path = it.get("path")
                    if path and os.path.exists(path):
                        os.remove(path)

    async def song(self, query: str, *, messenger: Messenger) -> None:
        filepath = None
        async with self._semaphore:
            await messenger.update("⬇️ Descargando canción...")
            loop = asyncio.get_running_loop()
            progress_cb = _progress_bridge(loop, messenger)
            try:
                filepath, meta = await loop.run_in_executor(None, download_song, query, progress_cb)
                title = meta["title"]
                artist = meta.get("artist")
                await messenger.update("📤 Preparando el archivo")
                await messenger.send_audio(
                    filepath, title=title, performer=artist,
                    filename=_audio_filename(title, artist),
                )
                await messenger.finish()
            finally:
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
