import asyncio
import logging
import os
from urllib.parse import urlparse

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

from config import BOT_TOKEN, MAX_TELEGRAM_SIZE_BYTES, MAX_PREFLIGHT_SIZE_BYTES, SUPPORTED_DOMAINS, MAX_CONCURRENT_DOWNLOADS
from database import init_db, upsert_user
from downloader import download_video, download_audio, get_video_dimensions, get_video_info, get_audio_info
from rate_limiter import rate_limiter

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# URL pendiente por usuario hasta que elija formato (video o audio)
_pending: dict[int, dict] = {}

# Límite de descargas simultáneas
_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

_YOUTUBE_DOMAINS = {"youtu.be", "youtube.com", "music.youtube.com"}


def _is_supported_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.netloc.lower().removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)
    except Exception:
        return False


def _is_youtube_url(text: str) -> bool:
    try:
        host = urlparse(text).netloc.lower().removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in _YOUTUBE_DOMAINS)
    except Exception:
        return False


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    upsert_user(user.id, user.username, user.first_name)
    await update.message.reply_text(
        "👋 ¡Hola! Envíame un link de TikTok, Instagram, Facebook, YouTube o X/Twitter "
        "y te descargo el video. 🎬"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 Plataformas soportadas:\n\n"
        "• 🎵 TikTok\n"
        "• 📸 Instagram (Reels y posts públicos)\n"
        "• 👥 Facebook (videos públicos)\n"
        "• ▶️ YouTube (video o MP3)\n"
        "• 🐦 X / Twitter (videos públicos)\n\n"
        "Solo envía el link y listo. ✅"
    )


_PHASE_MESSAGES = {
    "downloading": "⬇️ Descargando...",
    "finished":    "🔄 Procesando...",
}


def _make_progress_callback(loop: asyncio.AbstractEventLoop, status_msg):
    last_phase = [None]

    async def _edit(text: str) -> None:
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    def callback(status: str) -> None:
        text = _PHASE_MESSAGES.get(status)
        if text is None or status == last_phase[0]:
            return
        last_phase[0] = status
        asyncio.run_coroutine_threadsafe(_edit(text), loop)

    return callback


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = update.message.text.strip()
    user = update.effective_user

    if not _is_supported_url(url):
        await update.message.reply_text(
            "❌ No reconozco ese link. Prueba con TikTok, Instagram, Facebook, YouTube o X/Twitter."
        )
        return

    if not rate_limiter.is_allowed(user.id):
        wait = rate_limiter.seconds_until_reset(user.id)
        await update.message.reply_text(
            f"⏱️ Vas muy rápido. Espera {wait} segundos antes de enviar otro link."
        )
        return

    upsert_user(user.id, user.username, user.first_name)

    status_msg = await update.message.reply_text("🔍 Verificando...")

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, get_video_info, url)
        filesize = info.get("filesize")

        is_youtube = _is_youtube_url(url)

        if filesize and filesize > MAX_PREFLIGHT_SIZE_BYTES:
            size_mb = filesize / (1024 * 1024)
            limit_mb = MAX_PREFLIGHT_SIZE_BYTES // (1024 * 1024)
            if is_youtube:
                _pending[user.id] = {"url": url, "status_msg": status_msg}
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎵 Audio (MP3)", callback_data="fmt:audio"),
                ]])
                await status_msg.edit_text(
                    f"⚠️ El video pesa ~{size_mb:.0f} MB y supera el límite de {limit_mb} MB.\n"
                    "¿Lo descargo como MP3?",
                    reply_markup=keyboard,
                )
            else:
                await status_msg.edit_text(
                    f"❌ El video pesa ~{size_mb:.0f} MB y supera el límite de {limit_mb} MB."
                )
            return

        if is_youtube:
            _pending[user.id] = {"url": url, "status_msg": status_msg}
            note = "🎵 Parece una canción." if info.get("is_music") else ""
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Video (MP4)", callback_data="fmt:video"),
                InlineKeyboardButton("🎵 Audio (MP3)", callback_data="fmt:audio"),
            ]])
            text = f"¿Cómo quieres descargarlo?{' ' + note if note else ''}"
            await status_msg.edit_text(text, reply_markup=keyboard)
            return

        await _do_download(url, status_msg, loop, fmt="video")

    except yt_dlp.DownloadError as e:
        logger.warning("DownloadError para %s: %s", url, e)
        await status_msg.edit_text(_download_error_msg(str(e)))
    except Exception:
        logger.exception("Error inesperado para %s", url)
        await status_msg.edit_text("💥 Ocurrió un error inesperado. Inténtalo de nuevo.")


async def handle_format_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    pending = _pending.pop(user.id, None)

    if not pending:
        await query.edit_message_text("⌛ Esta selección expiró. Envía el link de nuevo.")
        return

    url = pending["url"]
    status_msg = query.message
    fmt = "audio" if query.data == "fmt:audio" else "video"

    await query.edit_message_reply_markup(reply_markup=None)

    loop = asyncio.get_running_loop()
    try:
        if fmt == "audio":
            await status_msg.edit_text("🔍 Verificando...")
            audio_info = await loop.run_in_executor(None, get_audio_info, url)
            audio_size = audio_info.get("filesize")
            if audio_size and audio_size > MAX_PREFLIGHT_SIZE_BYTES:
                size_mb = audio_size / (1024 * 1024)
                limit_mb = MAX_PREFLIGHT_SIZE_BYTES // (1024 * 1024)
                await status_msg.edit_text(
                    f"❌ El audio pesa ~{size_mb:.0f} MB y supera el límite de {limit_mb} MB."
                )
                return
        await _do_download(url, status_msg, loop, fmt=fmt)
    except yt_dlp.DownloadError as e:
        logger.warning("DownloadError para %s: %s", url, e)
        await status_msg.edit_text(_download_error_msg(str(e)))
    except Exception:
        logger.exception("Error inesperado para %s", url)
        await status_msg.edit_text("💥 Ocurrió un error inesperado. Inténtalo de nuevo.")


async def _do_download(url: str, status_msg, loop, fmt: str) -> None:
    await status_msg.edit_text("⏳ En cola...")
    filepath = None

    async with _download_semaphore:
        await status_msg.edit_text("⬇️ Descargando...")
        progress_cb = _make_progress_callback(loop, status_msg)

        try:
            if fmt == "audio":
                filepath, meta = await loop.run_in_executor(None, download_audio, url, progress_cb)
                title = meta["title"]
                artist = meta.get("artist")
                audio_filename = f"{artist} - {title}.mp3" if artist else f"{title}.mp3"
                await status_msg.edit_text("📤 Enviando audio...")
                with open(filepath, "rb") as f:
                    await status_msg.reply_audio(
                        audio=f,
                        title=title,
                        performer=artist,
                        filename=audio_filename,
                    )
            else:
                filepath = await loop.run_in_executor(None, download_video, url, progress_cb)
                file_size = os.path.getsize(filepath)
                width, height = get_video_dimensions(filepath)

                if file_size > MAX_TELEGRAM_SIZE_BYTES:
                    await status_msg.edit_text("📦 El video es grande, enviando como documento...")
                    with open(filepath, "rb") as f:
                        await status_msg.reply_document(document=f)
                else:
                    await status_msg.edit_text("📤 Enviando video...")
                    with open(filepath, "rb") as f:
                        await status_msg.reply_video(
                            video=f,
                            width=width or None,
                            height=height or None,
                            supports_streaming=True,
                        )

            await status_msg.delete()

        finally:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)


def _download_error_msg(reason: str) -> str:
    reason = reason.lower()
    if "private" in reason or "login" in reason:
        return "🔒 No puedo descargar ese video, parece que es privado o requiere login."
    if "not found" in reason or "404" in reason:
        return "🔍 No encontré el video. Verifica que el link sea correcto."
    return "⚠️ No pude descargar el video. Puede que sea privado o que el link haya expirado."


def main() -> None:
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_format_choice, pattern="^fmt:"))

    logger.info("Bot iniciado")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
