import asyncio
import logging
import os
import uuid
from functools import wraps
from urllib.parse import urlparse

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeDefault, BotCommandScopeChat, InputMediaPhoto, InputMediaVideo
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

from config import BOT_TOKEN, MAX_TELEGRAM_SIZE_BYTES, MAX_PREFLIGHT_SIZE_BYTES, SUPPORTED_DOMAINS, MAX_CONCURRENT_DOWNLOADS, ADMIN_CHAT_ID, HEALTH_PORT, MAX_VIDEO_HEIGHT, MAX_COMPRESS_HEIGHT, WEBHOOK_URL, WEBHOOK_SECRET, PORT
from database import init_db, upsert_user, get_all_users, get_stats, get_user_max_resolution, set_user_max_resolution, clear_user_max_resolution
from downloader import download_video, download_audio, download_song, download_post, compress_video, get_video_dimensions, get_video_info, get_audio_info, _IMAGE_EXTS
from rate_limiter import rate_limiter

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx loguea cada request en INFO, incluyendo la URL completa con el BOT_TOKEN.
# Subimos su nivel a WARNING para que el token no quede expuesto en los logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# URL pendiente por usuario hasta que elija formato (video o audio)
_pending: dict[int, dict] = {}

# Límite de descargas simultáneas
_download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Update IDs de mensajes duplicados detectados en la cola al arrancar
_duplicate_update_ids: set[int] = set()

# token → query de búsqueda para el botón "Descargar canción" (callback_data ≤ 64 bytes).
_song_queries: dict[str, str] = {}
_SONG_STORE_MAX = 500

# Telegram permite hasta 10 elementos por álbum (media group).
_MEDIA_GROUP_MAX = 10

_YOUTUBE_DOMAINS = {"youtu.be", "youtube.com", "music.youtube.com"}


def _store_song(query: str) -> str:
    token = uuid.uuid4().hex[:16]
    if len(_song_queries) >= _SONG_STORE_MAX:
        _song_queries.pop(next(iter(_song_queries)))  # evicta el más antiguo
    _song_queries[token] = query
    return token


def _song_keyboard(song: dict | None) -> InlineKeyboardMarkup | None:
    """Botón para descargar la canción identificada, o None si no hay canción."""
    if not song:
        return None
    artist, track = song["artist"], song["track"]
    token = _store_song(f"{artist} {track}")
    label = f"🎵 Descargar: {artist} - {track}"
    if len(label) > 60:
        label = label[:57] + "…"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"song:{token}"),
    ]])


def _extract_urls(text: str) -> list[str]:
    """Devuelve todas las URLs soportadas del mensaje, sin duplicados y en orden."""
    urls: list[str] = []
    seen: set[str] = set()
    for token in text.split():
        if token not in seen and _is_supported_url(token):
            seen.add(token)
            urls.append(token)
    return urls


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


async def _health_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.read(1024)
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK"
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


_RESOLUTION_OPTIONS = [360, 480, 720, 1080]


def _resolution_keyboard(current: int | None) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(_RESOLUTION_OPTIONS), 2):
        row = []
        for r in _RESOLUTION_OPTIONS[i:i + 2]:
            label = f"✅ {r}p" if r == current else f"{r}p"
            row.append(InlineKeyboardButton(label, callback_data=f"settings:res:{r}"))
        rows.append(row)
    default_label = "✅ Por defecto" if current is None else "Por defecto"
    rows.append([InlineKeyboardButton(default_label, callback_data="settings:res:default")])
    return InlineKeyboardMarkup(rows)


_PUBLIC_COMMANDS = [
    BotCommand("start", "Iniciar el bot"),
    BotCommand("help", "Ver plataformas soportadas"),
    BotCommand("settings", "⚙️ Configuración"),
]

_ADMIN_COMMANDS = _PUBLIC_COMMANDS + [
    BotCommand("users", "👥 Listar usuarios registrados"),
    BotCommand("stats", "📊 Estadísticas de uso"),
]


async def post_init(application) -> None:
    # Registrar comandos visibles según el scope
    await application.bot.set_my_commands(_PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    if ADMIN_CHAT_ID:
        await application.bot.set_my_commands(
            _ADMIN_COMMANDS,
            scope=BotCommandScopeChat(chat_id=int(ADMIN_CHAT_ID)),
        )

    # Health server: solo en modo polling. En modo webhook, PTB ya ocupa el puerto
    # con su propio servidor HTTP, así que un segundo servidor chocaría.
    if not WEBHOOK_URL:
        try:
            await asyncio.start_server(_health_handler, "0.0.0.0", HEALTH_PORT)
            logger.info("Health server escuchando en el puerto %d", HEALTH_PORT)
        except Exception:
            logger.warning("No se pudo iniciar el health server en el puerto %d", HEALTH_PORT)

    # Peek de la cola de mensajes pendientes (sin consumirla)
    try:
        pending = await application.bot.get_updates(timeout=0, limit=200)
    except Exception:
        logger.warning("No se pudo consultar la cola de updates pendientes")
        pending = []

    # Detectar links duplicados dentro de la cola
    seen_urls: dict[str, int] = {}  # url → primer update_id
    dup_count = 0
    for upd in pending:
        if upd.message and upd.message.text:
            url = upd.message.text.strip()
            if _is_supported_url(url):
                if url in seen_urls:
                    _duplicate_update_ids.add(upd.update_id)
                    dup_count += 1
                else:
                    seen_urls[url] = upd.update_id

    # Notificar al admin
    if ADMIN_CHAT_ID:
        try:
            if not pending:
                text = "🤖 Bot iniciado."
            else:
                text = f"🤖 Bot reiniciado.\n📬 {len(pending)} mensajes pendientes en cola."
                if dup_count:
                    text += f"\n🔁 {dup_count} links duplicados detectados y descartados."
            await application.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
        except Exception:
            logger.warning("No se pudo enviar notificación al admin")


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


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ADMIN_CHAT_ID or str(update.effective_user.id) != str(ADMIN_CHAT_ID):
            await update.message.reply_text("⛔ Este comando es solo para administradores.")
            return
        return await func(update, context)
    return wrapper


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    current = get_user_max_resolution(user.id)
    display = f"{current}p" if current else f"Por defecto ({MAX_VIDEO_HEIGHT}p)"
    await update.message.reply_text(
        f"⚙️ *Configuración*\n\nResolución máxima actual: *{display}*\n\nSelecciona la resolución máxima para tus descargas:",
        reply_markup=_resolution_keyboard(current),
        parse_mode="Markdown",
    )


async def handle_settings_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    value = query.data.split(":")[2]

    if value == "default":
        clear_user_max_resolution(user.id)
        current = None
    else:
        try:
            height = int(value)
        except ValueError:
            await query.answer("Opción no válida.", show_alert=True)
            return
        if height not in _RESOLUTION_OPTIONS:
            await query.answer("Opción no válida.", show_alert=True)
            return
        set_user_max_resolution(user.id, height)
        current = height

    display = f"{current}p" if current else f"Por defecto ({MAX_VIDEO_HEIGHT}p)"
    await query.edit_message_text(
        f"⚙️ *Configuración*\n\nResolución máxima: *{display}* ✅\n\nSelecciona la resolución máxima para tus descargas:",
        reply_markup=_resolution_keyboard(current),
        parse_mode="Markdown",
    )


@admin_only
async def cmd_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    page = int(args[0]) if args and args[0].isdigit() else 1

    users, total = get_all_users(page=page, page_size=20)
    total_pages = max(1, (total + 19) // 20)

    if not users:
        await update.message.reply_text("👥 No hay usuarios registrados.")
        return

    lines = [f"👥 Usuarios registrados: {total}\n"]
    for i, u in enumerate(users, start=(page - 1) * 20 + 1):
        username = f"@{u['username']}" if u["username"] else "—"
        name = u["first_name"] or "—"
        date = str(u["last_seen"])[:10]
        lines.append(f"{i}. {name} ({username}) · {u['total_requests']} req · {date}")

    if total_pages > 1:
        lines.append(f"\nPágina {page}/{total_pages}")
        if page < total_pages:
            lines.append(f"/users {page + 1} para ver más →")

    await update.message.reply_text("\n".join(lines))


@admin_only
async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    stats = get_stats()
    await update.message.reply_text(
        f"📊 Estadísticas\n\n"
        f"👥 Usuarios registrados: {stats['total_users']}\n"
        f"📥 Total descargas: {stats['total_requests']}"
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
    if update.update_id in _duplicate_update_ids:
        _duplicate_update_ids.discard(update.update_id)
        await update.message.reply_text(
            "🔁 Este link ya fue solicitado en la cola mientras el bot estaba reiniciando. "
            "Está siendo procesado; si no recibes el resultado, envíalo de nuevo."
        )
        return

    user = update.effective_user
    urls = _extract_urls(update.message.text)

    if not urls:
        await update.message.reply_text(
            "❌ No reconozco ese link. Prueba con TikTok, Instagram, Facebook, YouTube o X/Twitter."
        )
        return

    # Rate limit: cada link cuenta como una solicitud independiente.
    allowed: list[str] = []
    for u in urls:
        if rate_limiter.is_allowed(user.id):
            allowed.append(u)
        else:
            break

    if not allowed:
        wait = rate_limiter.seconds_until_reset(user.id)
        await update.message.reply_text(
            f"⏱️ Vas muy rápido. Espera {wait} segundos antes de enviar otro link."
        )
        return

    if len(allowed) < len(urls):
        await update.message.reply_text(
            f"⏱️ Proceso {len(allowed)} de {len(urls)} links; el resto supera tu límite por ahora."
        )

    user_pref = get_user_max_resolution(user.id)
    multi = len(allowed) > 1

    # Con varios links, YouTube va directo a video para no chocar con el slot único de _pending.
    for u in allowed:
        upsert_user(user.id, user.username, user.first_name)
        await _process_url(update, u, user_pref, allow_format_choice=not multi)


async def _process_url(update: Update, url: str, user_pref: int | None, allow_format_choice: bool) -> None:
    user = update.effective_user
    status_msg = await update.message.reply_text("🔍 Verificando...")

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, get_video_info, url, user_pref or MAX_VIDEO_HEIGHT)

        # Carrusel (varios elementos) o post de una sola foto: se descarga completo y se
        # envía como álbum/foto. Ambos pasan por la misma ruta (download_post baja las
        # fotos vía thumbnail y los videos con su formato).
        if (info.get("is_playlist") and info.get("count", 1) > 1) or info.get("is_image"):
            await _do_carousel(url, status_msg, loop, user_pref)
            return

        filesize = info.get("filesize")
        is_youtube = _is_youtube_url(url)

        if filesize and filesize > MAX_PREFLIGHT_SIZE_BYTES:
            size_mb = filesize / (1024 * 1024)
            limit_mb = MAX_PREFLIGHT_SIZE_BYTES // (1024 * 1024)
            if is_youtube and allow_format_choice:
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

        if is_youtube and allow_format_choice:
            _pending[user.id] = {"url": url, "status_msg": status_msg, "song": info.get("song")}
            note = "🎵 Parece una canción." if info.get("is_music") else ""
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Video (MP4)", callback_data="fmt:video"),
                InlineKeyboardButton("🎵 Audio (MP3)", callback_data="fmt:audio"),
            ]])
            text = f"¿Cómo quieres descargarlo?{' ' + note if note else ''}"
            await status_msg.edit_text(text, reply_markup=keyboard)
            return

        await _do_download(url, status_msg, loop, fmt="video", user_pref=user_pref, song=info.get("song"))

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
    song = pending.get("song")
    user_pref = get_user_max_resolution(user.id)
    status_msg = query.message
    fmt = "audio" if query.data == "fmt:audio" else "video"

    await query.edit_message_reply_markup(reply_markup=None)

    loop = asyncio.get_running_loop()
    try:
        if fmt == "audio":
            await status_msg.edit_text("🔍 Verificando...")
            if await _audio_over_limit(url, status_msg, loop):
                return
        await _do_download(url, status_msg, loop, fmt=fmt, user_pref=user_pref, song=song)
    except yt_dlp.DownloadError as e:
        logger.warning("DownloadError para %s: %s", url, e)
        await status_msg.edit_text(_download_error_msg(str(e)))
    except Exception:
        logger.exception("Error inesperado para %s", url)
        await status_msg.edit_text("💥 Ocurrió un error inesperado. Inténtalo de nuevo.")


async def _audio_over_limit(url: str, status_msg, loop) -> bool:
    """Verifica el tamaño del audio. Si supera el límite, avisa y devuelve True."""
    audio_info = await loop.run_in_executor(None, get_audio_info, url)
    audio_size = audio_info.get("filesize")
    if audio_size and audio_size > MAX_PREFLIGHT_SIZE_BYTES:
        size_mb = audio_size / (1024 * 1024)
        limit_mb = MAX_PREFLIGHT_SIZE_BYTES // (1024 * 1024)
        await status_msg.edit_text(
            f"❌ El audio pesa ~{size_mb:.0f} MB y supera el límite de {limit_mb} MB."
        )
        return True
    return False


async def handle_song_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    token = query.data.split(":", 1)[1]
    search = _song_queries.pop(token, None)
    if not search:
        await query.answer("⌛ Esta opción expiró. Envía el link de nuevo.", show_alert=True)
        return

    # Quita el botón para evitar doble toque.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    status_msg = await query.message.reply_text(f"🔎 Buscando: {search}...")
    try:
        await _do_song_download(search, status_msg, loop)
    except yt_dlp.DownloadError as e:
        logger.warning("DownloadError (canción) para %s: %s", search, e)
        await status_msg.edit_text("⚠️ No encontré esa canción. Intenta con otra.")
    except Exception:
        logger.exception("Error inesperado descargando canción %s", search)
        await status_msg.edit_text("💥 Ocurrió un error inesperado. Inténtalo de nuevo.")


async def _do_song_download(query: str, status_msg, loop) -> None:
    filepath = None
    async with _download_semaphore:
        await status_msg.edit_text("⬇️ Descargando canción...")
        progress_cb = _make_progress_callback(loop, status_msg)
        try:
            filepath, meta = await loop.run_in_executor(None, download_song, query, progress_cb)
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
            await status_msg.delete()
        finally:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)


async def _do_download(url: str, status_msg, loop, fmt: str, user_pref: int | None = None, song: dict | None = None) -> None:
    await status_msg.edit_text("⏳ En cola...")
    filepath = None
    extra_path = None  # archivo comprimido temporal, si se genera
    effective_height = user_pref or MAX_VIDEO_HEIGHT

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
                filepath = await loop.run_in_executor(None, download_video, url, progress_cb, effective_height)

                # Post de una sola foto (link de imagen, no video): enviar como foto.
                if filepath.rsplit(".", 1)[-1].lower() in _IMAGE_EXTS:
                    await status_msg.edit_text("📤 Enviando foto...")
                    with open(filepath, "rb") as f:
                        await status_msg.reply_photo(photo=f)
                    await status_msg.delete()
                    return

                file_size = os.path.getsize(filepath)
                width, height = get_video_dimensions(filepath)

                quality_note = None
                if user_pref and height:
                    if height < user_pref:
                        quality_note = f"📐 Solo disponible en {height}p (tu preferencia: {user_pref}p)"
                    elif height > user_pref:
                        quality_note = f"📐 Descargado en {height}p — no había formatos disponibles en {user_pref}p o menos"

                song_kb = _song_keyboard(song)
                send_path = filepath
                as_document = False

                if file_size > MAX_TELEGRAM_SIZE_BYTES:
                    # Intenta comprimir para poder enviarlo como video reproducible.
                    await status_msg.edit_text("🗜️ El video es grande, comprimiendo...")
                    compressed = await loop.run_in_executor(
                        None, compress_video, filepath, MAX_TELEGRAM_SIZE_BYTES, MAX_COMPRESS_HEIGHT
                    )
                    if compressed:
                        extra_path = compressed
                        send_path = compressed
                        width, height = get_video_dimensions(send_path)
                    else:
                        as_document = True  # la compresión no bastó: cae a documento

                if as_document:
                    await status_msg.edit_text("📦 No se pudo comprimir, enviando como documento...")
                    with open(filepath, "rb") as f:
                        await status_msg.reply_document(document=f, caption=quality_note, reply_markup=song_kb)
                else:
                    await status_msg.edit_text("📤 Enviando video...")
                    with open(send_path, "rb") as f:
                        await status_msg.reply_video(
                            video=f,
                            width=width or None,
                            height=height or None,
                            supports_streaming=True,
                            caption=quality_note,
                            reply_markup=song_kb,
                        )

            await status_msg.delete()

        finally:
            for path in (filepath, extra_path):
                if path and os.path.exists(path):
                    os.remove(path)


async def _do_carousel(url: str, status_msg, loop, user_pref: int | None = None) -> None:
    await status_msg.edit_text("⏳ En cola...")
    items: list[dict] = []
    effective_height = user_pref or MAX_VIDEO_HEIGHT

    async with _download_semaphore:
        await status_msg.edit_text("⬇️ Descargando...")
        progress_cb = _make_progress_callback(loop, status_msg)

        try:
            items = await loop.run_in_executor(None, download_post, url, progress_cb, effective_height)
            if not items:
                await status_msg.edit_text("⚠️ No pude descargar el contenido de ese post.")
                return

            await status_msg.edit_text(f"📤 Enviando {len(items)} elementos...")
            await _send_media_groups(status_msg, items)
            await status_msg.delete()

        finally:
            for it in items:
                path = it.get("path")
                if path and os.path.exists(path):
                    os.remove(path)


async def _send_media_groups(status_msg, items: list[dict]) -> None:
    """Envía los items de un carrusel en álbumes de hasta 10 elementos."""
    for i in range(0, len(items), _MEDIA_GROUP_MAX):
        chunk = items[i:i + _MEDIA_GROUP_MAX]
        open_files = []
        try:
            media = []
            for it in chunk:
                f = open(it["path"], "rb")
                open_files.append(f)
                if it["kind"] == "photo":
                    media.append(InputMediaPhoto(f))
                else:
                    media.append(InputMediaVideo(f, supports_streaming=True))
            await status_msg.reply_media_group(media=media)
        finally:
            for f in open_files:
                f.close()


def _download_error_msg(reason: str) -> str:
    reason = reason.lower()
    if "private" in reason or "login" in reason:
        return "🔒 No puedo descargar ese video, parece que es privado o requiere login."
    if "not found" in reason or "404" in reason:
        return "🔍 No encontré el video. Verifica que el link sea correcto."
    if "unable to extract" in reason or "rehydration" in reason:
        return "🛠️ No pude leer ese video ahora mismo (la plataforma cambió algo). Intenta de nuevo en un rato."
    return "⚠️ No pude descargar el video. Puede que sea privado o que el link haya expirado."


def main() -> None:
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("users", cmd_admin_users))
    app.add_handler(CommandHandler("stats", cmd_admin_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.add_handler(CallbackQueryHandler(handle_format_choice, pattern="^fmt:"))
    app.add_handler(CallbackQueryHandler(handle_song_download, pattern="^song:"))
    app.add_handler(CallbackQueryHandler(handle_settings_choice, pattern="^settings:res:"))

    if WEBHOOK_URL:
        # En Render (o cualquier host con URL pública) el bot recibe los mensajes
        # por webhook: Telegram hace una petición HTTP entrante, lo que despierta
        # el servicio si estaba dormido. El path usa el token para ofuscar el endpoint.
        logger.info("Bot iniciado en modo webhook")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}",
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=False,
        )
    else:
        logger.info("Bot iniciado en modo polling")
        app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
