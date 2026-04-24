import asyncio
import logging
import os
from urllib.parse import urlparse

import yt_dlp
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

from config import BOT_TOKEN, MAX_TELEGRAM_SIZE_BYTES, SUPPORTED_DOMAINS
from database import init_db, upsert_user
from downloader import download_video, get_video_dimensions
from rate_limiter import rate_limiter

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _is_supported_url(text: str) -> bool:
    try:
        parsed = urlparse(text)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.netloc.lower().removeprefix("www.")
        return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)
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
        "• ▶️ YouTube\n"
        "• 🐦 X / Twitter (videos públicos)\n\n"
        "Solo envía el link y listo. ✅"
    )


_PHASE_MESSAGES = {
    "downloading": "⬇️ Descargando...",
    "finished":    "🔄 Procesando video...",
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

    status_msg = await update.message.reply_text("⏳ Descargando... un momento")

    filepath = None
    try:
        loop = asyncio.get_running_loop()
        progress_cb = _make_progress_callback(loop, status_msg)
        filepath = await loop.run_in_executor(None, download_video, url, progress_cb)

        file_size = os.path.getsize(filepath)
        width, height = get_video_dimensions(filepath)

        if file_size > MAX_TELEGRAM_SIZE_BYTES:
            await status_msg.edit_text("📦 El video es grande, enviando como documento...")
            with open(filepath, "rb") as f:
                await update.message.reply_document(document=f)
        else:
            await status_msg.edit_text("📤 Enviando video...")
            with open(filepath, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    width=width or None,
                    height=height or None,
                    supports_streaming=True,
                )

        await status_msg.delete()

    except yt_dlp.DownloadError as e:
        logger.warning("DownloadError para %s: %s", url, e)
        reason = str(e).lower()
        if "private" in reason or "login" in reason:
            msg = "🔒 No puedo descargar ese video, parece que es privado o requiere login."
        elif "not found" in reason or "404" in reason:
            msg = "🔍 No encontré el video. Verifica que el link sea correcto."
        else:
            msg = "⚠️ No pude descargar el video. Puede que sea privado o que el link haya expirado."
        await status_msg.edit_text(msg)

    except Exception as e:
        logger.exception("Error inesperado para %s", url)
        await status_msg.edit_text("💥 Ocurrió un error inesperado. Inténtalo de nuevo.")

    finally:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)


def main() -> None:
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))

    logger.info("Bot iniciado")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
