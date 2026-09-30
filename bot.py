import asyncio
import logging
import time
import uuid
from functools import wraps

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, BotCommandScopeDefault, BotCommandScopeChat, InputMediaPhoto, InputMediaVideo
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

from config import EXECUTOR_MAX_WORKERS, TRANSCODE_MAX_DURATION, BOT_TOKEN, MAX_TELEGRAM_SIZE_BYTES, MAX_PREFLIGHT_SIZE_BYTES, MAX_CONCURRENT_DOWNLOADS, ADMIN_CHAT_ID, HEALTH_PORT, MAX_VIDEO_HEIGHT, MAX_COMPRESS_HEIGHT, BEST_QUALITY_HEIGHT, WEBHOOK_URL, WEBHOOK_SECRET, PORT, NOTIFY_ON_START, WEB_URL
from database import init_db, upsert_user, get_all_users, get_stats, get_user_max_resolution, set_user_max_resolution, clear_user_max_resolution
from downloader import (check_youtube_config, get_video_info, get_audio_info, purge_temp_dir,
                        transcode_allowed, VideoConversionError)
from links import extract_urls, is_supported_url, is_youtube_url
from pipeline import DeliveryLimits, Pipeline, download_error_message, install_bounded_executor
from rate_limiter import rate_limiter
from version import get_local_version, check_remote, uptime_str

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx loguea cada request en INFO, incluyendo la URL completa con el BOT_TOKEN.
# Subimos su nivel a WARNING para que el token no quede expuesto en los logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# URL pendiente por usuario hasta que elija formato (video o audio).
# Un slot por usuario, y con caducidad: si nadie pulsa el botón la entrada no se
# reclamaba nunca, y se quedaba reteniendo el objeto Message de PTB. Con un slot por
# usuario eso crece con la base de usuarios, sin techo. El teclado, además, ya deja de
# valer solo (handle_format_choice contesta "esa opción venció"), así que guardarlo más
# tiempo del que el usuario puede usarlo no servía para nada.
_pending: dict[int, dict] = {}
_PENDING_TTL_SECONDS = 15 * 60


def _set_pending(user_id: int, data: dict) -> None:
    """Guarda la elección pendiente del usuario y barre las que ya vencieron."""
    ahora = time.monotonic()
    for uid, pend in list(_pending.items()):
        if ahora - pend.get("created", ahora) > _PENDING_TTL_SECONDS:
            del _pending[uid]
    data["created"] = ahora
    _pending[user_id] = data


def _take_pending(user_id: int) -> dict | None:
    """Saca la elección pendiente, o None si no hay o si ya caducó."""
    pend = _pending.pop(user_id, None)
    if pend is None:
        return None
    if time.monotonic() - pend.get("created", 0) > _PENDING_TTL_SECONDS:
        return None
    return pend

# Orquesta las descargas contra el motor; el límite de concurrencia vive dentro (ver
# pipeline.py). Es el único punto de entrada al motor, así que su límite de descargas
# simultáneas protege al host sin importar cuántos canales lo compartan en el futuro.
pipeline = Pipeline(MAX_CONCURRENT_DOWNLOADS, MAX_VIDEO_HEIGHT)

# Los topes de Telegram traducidos al lenguaje del pipeline: por encima de
# max_inline_bytes se intenta comprimir antes de caer a documento.
DELIVERY_LIMITS = DeliveryLimits(
    max_inline_bytes=MAX_TELEGRAM_SIZE_BYTES,
    max_compress_height=MAX_COMPRESS_HEIGHT,
)

# Update IDs de mensajes duplicados detectados en la cola al arrancar
_duplicate_update_ids: set[int] = set()

# token → query de búsqueda para el botón "Descargar canción" (callback_data ≤ 64 bytes).
_song_queries: dict[str, str] = {}
_SONG_STORE_MAX = 500


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
    label = f"🎵 Bajar: {artist} - {track}"
    if len(label) > 60:
        label = label[:57] + "…"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"song:{token}"),
    ]])


# Telegram permite hasta 10 elementos por álbum (media group).
_MEDIA_GROUP_MAX = 10


class TelegramMessenger:
    """
    Adaptador de pipeline.Messenger para python-telegram-bot: el status_msg ya existe
    (se crea durante el routing, antes de invocar el pipeline) y esta clase solo lo
    envuelve. Es el único lugar donde el pipeline toca la API de Telegram.
    """

    def __init__(self, status_msg, limits: DeliveryLimits):
        self._msg = status_msg
        self.limits = limits

    async def update(self, text: str) -> None:
        await self._msg.edit_text(text)

    async def finish(self) -> None:
        await self._msg.delete()

    async def send_video(self, path, *, width, height, caption, song):
        with open(path, "rb") as f:
            await self._msg.reply_video(
                video=f, width=width, height=height, supports_streaming=True,
                caption=caption, reply_markup=_song_keyboard(song),
            )

    async def send_audio(self, path, *, title, performer, filename):
        with open(path, "rb") as f:
            await self._msg.reply_audio(audio=f, title=title, performer=performer, filename=filename)

    async def send_photo(self, path):
        with open(path, "rb") as f:
            await self._msg.reply_photo(photo=f)

    async def send_document(self, path, *, caption, song):
        with open(path, "rb") as f:
            await self._msg.reply_document(document=f, caption=caption, reply_markup=_song_keyboard(song))

    async def send_album(self, items: list[dict]) -> None:
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
                await self._msg.reply_media_group(media=media)
            finally:
                for f in open_files:
                    f.close()


async def _health_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.read(1024)
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nOK"
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


_RESOLUTION_OPTIONS = [360, 480, 720, 1080]


def _is_best_quality(user_pref: int | None) -> bool:
    """
    True si el usuario eligió "máxima calidad". Se guarda como BEST_QUALITY_HEIGHT en
    la misma columna que la resolución: el bot solo ofrece hasta 1080p, así que el
    valor no choca con ninguna opción real y evitó una columna nueva en la BD.
    """
    return user_pref == BEST_QUALITY_HEIGHT


def _quality_display(current: int | None) -> str:
    if _is_best_quality(current):
        return "Máxima calidad"
    return f"{current}p" if current else f"Por defecto ({MAX_VIDEO_HEIGHT}p)"


def _transcode_note() -> str:
    """
    Aviso en /settings cuando este servidor no puede convertir (o casi). Vale la pena
    decirlo ahí: si no, quien active máxima calidad recibiría siempre la versión
    compatible sin entender por qué, y la alternativa —avisarlo en cada descarga— sería
    ruido en todas las que no tienen nada que ver.
    """
    if TRANSCODE_MAX_DURATION >= 3600:
        return ""
    if not TRANSCODE_MAX_DURATION:
        return ("\n\n⚠️ Este servidor no tiene CPU para convertir, así que máxima calidad "
                "solo cambia algo cuando el mejor formato ya es compatible.")
    return (f"\n\n⚠️ Este servidor solo puede convertir videos de hasta "
            f"{TRANSCODE_MAX_DURATION}s; por encima recibes la versión compatible.")


def _conversion_error_message(best_quality: bool) -> str:
    """
    Qué decirle al usuario cuando el video bajó pero no se pudo convertir. Suele ser el
    recode quedándose sin memoria, y en modo máxima calidad es lo esperable: ahí se baja
    a propósito el formato que hay que convertir.
    """
    extra = (
        "\n\nTienes activada 🔝 Máxima calidad, que obliga a convertir. Si pones 1080p "
        "o menos en /settings, casi todo baja ya listo y no hace falta."
        if best_quality else ""
    )
    return ("😵 Bajé el video pero no pude dejarlo en un formato que Telegram reproduzca, "
            "así que prefiero no mandártelo roto." + extra)


def _resolution_keyboard(current: int | None) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(_RESOLUTION_OPTIONS), 2):
        row = []
        for r in _RESOLUTION_OPTIONS[i:i + 2]:
            label = f"✅ {r}p" if r == current else f"{r}p"
            row.append(InlineKeyboardButton(label, callback_data=f"settings:res:{r}"))
        rows.append(row)
    best_label = "✅ 🔝 Máxima calidad" if _is_best_quality(current) else "🔝 Máxima calidad"
    rows.append([InlineKeyboardButton(best_label, callback_data="settings:res:best")])
    default_label = "✅ Por defecto" if current is None else "Por defecto"
    rows.append([InlineKeyboardButton(default_label, callback_data="settings:res:default")])
    return InlineKeyboardMarkup(rows)


_PUBLIC_COMMANDS = [
    BotCommand("start", "Qué es y cómo se usa"),
    BotCommand("help", "Qué puedo descargar"),
    BotCommand("settings", "Resolución máxima"),
]

_ADMIN_COMMANDS = _PUBLIC_COMMANDS + [
    BotCommand("users", "👥 Listar usuarios registrados"),
    BotCommand("stats", "📊 Estadísticas de uso"),
    BotCommand("version", "🏷️ Versión/commit en ejecución"),
]


async def post_init(application) -> None:
    # Techo de hilos para todo el trabajo síncrono (yt-dlp, ffmpeg). Ver config.
    install_bounded_executor(EXECUTOR_MAX_WORKERS)

    # Lo que quedó de la ejecución anterior no lo va a reclamar nadie: los estados viven
    # en memoria y se fueron con el proceso. Si murió a mitad de una descarga (OOM,
    # deploy), el archivo sigue ahí ocupando disco.
    purge_temp_dir()

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

    # Peek de la cola de mensajes pendientes. Solo sirve en polling: en modo webhook,
    # getUpdates es incompatible con el webhook activo (siempre vacío/error), así que
    # se omite por completo.
    pending = []
    dup_count = 0
    if not WEBHOOK_URL:
        try:
            pending = await application.bot.get_updates(timeout=0, limit=200)
        except Exception:
            logger.warning("No se pudo consultar la cola de updates pendientes")
            pending = []

        # Detectar links duplicados dentro de la cola
        seen_urls: dict[str, int] = {}  # url → primer update_id
        for upd in pending:
            if upd.message and upd.message.text:
                url = upd.message.text.strip()
                if is_supported_url(url):
                    if url in seen_urls:
                        _duplicate_update_ids.add(upd.update_id)
                        dup_count += 1
                    else:
                        seen_urls[url] = upd.update_id

    # Notificar al admin solo si NOTIFY_ON_START está activado. En Render free el bot
    # arranca de cero en cada despertar del sleep, así que por defecto no se notifica
    # para no spamear al admin en cada cold start.
    if ADMIN_CHAT_ID and NOTIFY_ON_START:
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
        "👋 Soy Videito.\n\n"
        "Mándame un link de TikTok, Instagram, Facebook, YouTube o X y te devuelvo "
        "el archivo aquí mismo: el video completo, o solo el audio en MP3. "
        "Sin marca de agua y sin salir del chat.\n\n"
        "/help — qué puedo bajar y qué no\n"
        "/settings — resolución máxima"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📋 Qué puedo descargar\n\n"
        "• TikTok — videos y fotos, sin marca de agua\n"
        "• Instagram — reels, posts y carruseles públicos\n"
        "• Facebook — videos públicos\n"
        "• YouTube — video o MP3, tú eliges\n"
        "• X / Twitter — videos públicos\n\n"
        "Mándame el link solo, sin más texto alrededor. Si mandas varios, "
        "los proceso uno por uno.\n\n"
        "Telegram no me deja enviar archivos de más de 50 MB. Cuando un video pasa "
        "de ahí te dejo el enlace a la web, que no tiene ese límite.\n\n"
        "/settings — resolución máxima"
    )


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not ADMIN_CHAT_ID or str(update.effective_user.id) != str(ADMIN_CHAT_ID):
            await update.message.reply_text("⛔ Ese comando es solo para el admin.")
            return
        return await func(update, context)
    return wrapper


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    current = get_user_max_resolution(user.id)
    display = _quality_display(current)
    await update.message.reply_text(
        f"⚙️ *Resolución máxima*\n\nAhora mismo: *{display}*\n\n"
        "Bajo la mejor calidad disponible hasta ese tope. Menos resolución = "
        "archivo más liviano y descarga más rápida.\n\n"
        "🔝 *Máxima calidad* baja el mejor formato que haya aunque venga en un códec "
        "que Telegram no reproduce (el 1080p de TikTok, por ejemplo): lo convierto yo. "
        "Tarda bastante más y con videos largos puede fallar." + _transcode_note(),
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
    elif value == "best":
        set_user_max_resolution(user.id, BEST_QUALITY_HEIGHT)
        current = BEST_QUALITY_HEIGHT
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

    display = _quality_display(current)
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


@admin_only
async def cmd_admin_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    loop = asyncio.get_running_loop()
    local = get_local_version()
    # La comparación con GitHub hace I/O de red: fuera del event loop.
    remote = await loop.run_in_executor(None, check_remote, local["sha"], local["branch"])

    branch = local["branch"] or "?"
    lines = ["🏷️ Versión en ejecución", ""]
    lines.append(f"Commit: {local['short'] or '¿?'} ({branch})")
    if local["commit_msg"]:
        lines.append(f"Mensaje: {local['commit_msg']}")
    lines.append(f"Fuente: {local['source']}")
    if local["dirty"]:
        lines.append("✏️ Working tree con cambios sin commitear (local)")
    lines.append(f"Arrancó hace: {uptime_str()}")
    lines.append("")

    if not remote.get("ok"):
        lines.append(f"🔍 No pude comparar con GitHub: {remote.get('reason', 'error')}")
    else:
        status = remote.get("status")
        latest_short = (remote.get("latest") or "")[:9]
        latest_msg = remote.get("latest_msg", "")
        if status == "identical":
            lines.append(f"✅ Al día — corres el último commit de {branch}")
        elif status == "ahead":
            # base=corriendo, head=último: ahead_by = commits que le faltan al bot
            lines.append(f"⚠️ Estás {remote.get('ahead_by')} commit(s) por detrás de origin/{branch}")
            lines.append(f"Último en GitHub: {latest_short} — {latest_msg}")
        elif status == "behind":
            lines.append(f"🧪 Corres {remote.get('behind_by')} commit(s) que aún NO están en origin/{branch} (sin pushear)")
        elif status == "diverged":
            lines.append(f"🔀 Divergiste de origin/{branch} (hay commits en ambos lados)")
        else:
            lines.append(f"🔍 Último en GitHub {branch}: {latest_short} — {latest_msg}")

    await update.message.reply_text("\n".join(lines))


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.update_id in _duplicate_update_ids:
        _duplicate_update_ids.discard(update.update_id)
        await update.message.reply_text(
            "🔁 Ese link ya estaba en la cola mientras me reiniciaba. Se está procesando; "
            "si no te llega nada, mándalo otra vez."
        )
        return

    user = update.effective_user
    urls = extract_urls(update.message.text)

    if not urls:
        await update.message.reply_text(
            "❌ Ese link no lo reconozco. Sirven TikTok, Instagram, Facebook, YouTube y X."
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
            f"⏱️ Vas muy rápido. Espera {wait} segundos."
        )
        return

    if len(allowed) < len(urls):
        await update.message.reply_text(
            f"⏱️ Proceso {len(allowed)} de {len(urls)} links; el resto pasa tu límite por ahora."
        )

    user_pref = get_user_max_resolution(user.id)
    multi = len(allowed) > 1

    # Con varios links, YouTube va directo a video para no chocar con el slot único de _pending.
    for u in allowed:
        upsert_user(user.id, user.username, user.first_name)
        await _process_url(update, u, user_pref, allow_format_choice=not multi)


async def _process_url(update: Update, url: str, user_pref: int | None, allow_format_choice: bool) -> None:
    user = update.effective_user
    status_msg = await update.message.reply_text("🔍 Verificando el enlace")

    best_quality = _is_best_quality(user_pref)

    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(
            None, get_video_info, url, user_pref or MAX_VIDEO_HEIGHT, best_quality,
        )

        # Convertir cuesta memoria y minutos, y no siempre compra resolución: en un Reel
        # de Instagram el H.264 combinado da el mismo 1080x1920 que el VP9 que habría que
        # recodificar. Cuando el preflight dice que empatan, se baja en modo normal — el
        # resultado es idéntico y llega sin pasar por ffmpeg. (Un worker viejo no manda el
        # campo: se asume que sí hace falta, que es el comportamiento de antes.)
        if best_quality and info.get("avc_matches_best", False):
            logger.info("Máxima calidad sin recode para %s: el H.264 ya da la mejor "
                        "resolución disponible", url)
            best_quality = False
        elif best_quality and not transcode_allowed(info.get("duration")):
            # El modo elegiría el formato de más resolución aunque haya que convertirlo, y
            # en este host la conversión no cabe (ver TRANSCODE_MAX_DURATION). Decidirlo
            # ACÁ, antes de descargar, es la diferencia entre entregar el video compatible
            # de una vez y lo que pasaba en producción: bajar el VP9, quemar el timeout de
            # 5 minutos del recode y volver a descargar desde cero por el fallback.
            logger.info("Máxima calidad degradada para %s: convertir %ss no cabe en este "
                        "host (TRANSCODE_MAX_DURATION=%s)", url, info.get("duration"),
                        TRANSCODE_MAX_DURATION)
            best_quality = False

        # Carrusel (varios elementos) o post de una sola foto: se descarga completo y se
        # envía como álbum/foto. Ambos pasan por la misma ruta (download_post baja las
        # fotos vía thumbnail y los videos con su formato).
        # El carrusel va siempre en modo normal: ahí un recode fallido no tiene reintento
        # y el item se perdería del álbum sin que el usuario sepa por qué.
        if (info.get("is_playlist") and info.get("count", 1) > 1) or info.get("is_image"):
            messenger = TelegramMessenger(status_msg, DELIVERY_LIMITS)
            await pipeline.carousel(url, messenger=messenger, user_pref_height=user_pref)
            return

        filesize = info.get("filesize")
        is_youtube = is_youtube_url(url)

        if filesize and filesize > MAX_PREFLIGHT_SIZE_BYTES:
            size_mb = filesize / (1024 * 1024)
            limit_mb = MAX_PREFLIGHT_SIZE_BYTES // (1024 * 1024)
            # Pasarse del límite era un callejón sin salida: el canal web no tiene ese
            # tope, así que el aviso ofrece el otro canal en vez de terminar en un error.
            # Si WEB_URL está vacía (despliegue sin canal web) el aviso queda como antes.
            web_note = (
                f"\n\nBájalo desde la web, que no tiene ese límite:\n{WEB_URL}"
                if WEB_URL else ""
            )
            if is_youtube and allow_format_choice:
                _set_pending(user.id, {"url": url, "status_msg": status_msg})
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎵 Solo audio (MP3)", callback_data="fmt:audio"),
                ]])
                # La pregunta va al final, pegada al botón que la contesta; el enlace
                # a la web queda en medio como la otra salida posible.
                await status_msg.edit_text(
                    f"⚖️ Ese video pesa ~{size_mb:.0f} MB y mi límite aquí son {limit_mb} MB."
                    + web_note + "\n\n¿O lo bajo como MP3?",
                    reply_markup=keyboard,
                )
            else:
                await status_msg.edit_text(
                    f"⚖️ Ese video pesa ~{size_mb:.0f} MB y mi límite aquí son {limit_mb} MB."
                    + web_note
                )
            return

        if is_youtube and allow_format_choice:
            _set_pending(user.id, {"url": url, "status_msg": status_msg,
                                   "song": info.get("song")})
            note = ("Parece una canción, así que el MP3 te va a llegar con título y artista."
                    if info.get("is_music") else "")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎬 Video (MP4)", callback_data="fmt:video"),
                InlineKeyboardButton("🎵 Solo audio (MP3)", callback_data="fmt:audio"),
            ]])
            text = f"¿Cómo lo quieres?{' ' + note if note else ''}"
            await status_msg.edit_text(text, reply_markup=keyboard)
            return

        messenger = TelegramMessenger(status_msg, DELIVERY_LIMITS)
        await pipeline.download(url, fmt="video", messenger=messenger, user_pref_height=user_pref,
                                song=info.get("song"), best_quality=best_quality)

    except VideoConversionError:
        logger.warning("Video no convertible para %s", url)
        await status_msg.edit_text(_conversion_error_message(best_quality))
    except yt_dlp.DownloadError as e:
        logger.warning("DownloadError para %s: %s", url, e)
        await status_msg.edit_text(download_error_message(str(e)))
    except Exception:
        logger.exception("Error inesperado para %s", url)
        await status_msg.edit_text("💥 Algo se rompió de mi lado. Prueba de nuevo.")


async def handle_format_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    pending = _take_pending(user.id)

    if not pending:
        await query.edit_message_text("⌛ Esa opción venció. Mándame el link de nuevo.")
        return

    url = pending["url"]
    song = pending.get("song")
    user_pref = get_user_max_resolution(user.id)
    status_msg = query.message
    fmt = "audio" if query.data == "fmt:audio" else "video"

    await query.edit_message_reply_markup(reply_markup=None)

    try:
        if fmt == "audio":
            await status_msg.edit_text("🔍 Verificando el enlace")
            if await _audio_over_limit(url, status_msg, asyncio.get_running_loop()):
                return
        messenger = TelegramMessenger(status_msg, DELIVERY_LIMITS)
        await pipeline.download(url, fmt=fmt, messenger=messenger, user_pref_height=user_pref,
                                song=song, best_quality=_is_best_quality(user_pref))
    except VideoConversionError:
        logger.warning("Video no convertible para %s", url)
        await status_msg.edit_text(_conversion_error_message(_is_best_quality(user_pref)))
    except yt_dlp.DownloadError as e:
        logger.warning("DownloadError para %s: %s", url, e)
        await status_msg.edit_text(download_error_message(str(e)))
    except Exception:
        logger.exception("Error inesperado para %s", url)
        await status_msg.edit_text("💥 Algo se rompió de mi lado. Prueba de nuevo.")


async def _audio_over_limit(url: str, status_msg, loop) -> bool:
    """Verifica el tamaño del audio. Si supera el límite, avisa y devuelve True."""
    audio_info = await loop.run_in_executor(None, get_audio_info, url)
    audio_size = audio_info.get("filesize")
    if audio_size and audio_size > MAX_PREFLIGHT_SIZE_BYTES:
        size_mb = audio_size / (1024 * 1024)
        limit_mb = MAX_PREFLIGHT_SIZE_BYTES // (1024 * 1024)
        await status_msg.edit_text(
            f"⚖️ Ese audio pesa ~{size_mb:.0f} MB y mi límite aquí son {limit_mb} MB."
        )
        return True
    return False


async def handle_song_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    token = query.data.split(":", 1)[1]
    search = _song_queries.pop(token, None)
    if not search:
        await query.answer("⌛ Esa opción venció. Mándame el link de nuevo.", show_alert=True)
        return

    # Quita el botón para evitar doble toque.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    status_msg = await query.message.reply_text(f"🔎 Buscando: {search}")
    try:
        messenger = TelegramMessenger(status_msg, DELIVERY_LIMITS)
        await pipeline.song(search, messenger=messenger)
    except yt_dlp.DownloadError as e:
        logger.warning("DownloadError (canción) para %s: %s", search, e)
        await status_msg.edit_text("🔎 No encontré esa canción. Prueba con otra.")
    except Exception:
        logger.exception("Error inesperado descargando canción %s", search)
        await status_msg.edit_text("💥 Algo se rompió de mi lado. Prueba de nuevo.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN no está configurada")
    # Avisos, no errores: una config de YouTube incompleta no impide arrancar, pero
    # verla acá evita descubrirla como un falso "bloqueo de YouTube" horas después.
    check_youtube_config()
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("users", cmd_admin_users))
    app.add_handler(CommandHandler("stats", cmd_admin_stats))
    app.add_handler(CommandHandler("version", cmd_admin_version))
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
