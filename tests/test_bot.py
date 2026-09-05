"""
Tests de bot.py: routing y presentación (qué camino toma un link, qué teclado se
muestra, qué mensaje de error se ve). La ejecución de la descarga en sí — comprimir,
elegir documento vs. video, limpiar temporales, el semáforo de concurrencia — vive en
pipeline.py y se prueba en test_pipeline.py.

La descarga real se simula parcheando las funciones del motor por su nombre en el
módulo que las usa (`bot.get_video_info`, `pipeline.download_video`, ...), dejando que
asyncio corra de verdad. Es más robusto que interceptar `asyncio.get_running_loop`: si
alguien mueve dónde se llama una función, el test sigue siendo válido en vez de quedar
apuntando a un mock que ya no intercepta nada.
"""
import os
import pytest
import yt_dlp
from unittest.mock import AsyncMock, MagicMock, patch

from bot import cmd_start, cmd_help, handle_link, handle_format_choice, handle_song_download, post_init, cmd_admin_users, cmd_admin_stats


@pytest.fixture(autouse=True)
def mock_db_and_rate_limiter():
    with patch("bot.upsert_user"), \
         patch("bot.get_user_max_resolution", return_value=None), \
         patch("bot.rate_limiter") as mock_rl:
        mock_rl.is_allowed.return_value = True
        mock_rl.seconds_until_reset.return_value = 30
        yield mock_rl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(text: str, update_id: int = 0) -> MagicMock:
    update = MagicMock()
    update.update_id = update_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.reply_video = AsyncMock()
    update.message.reply_document = AsyncMock()
    return update


def _make_context() -> MagicMock:
    return MagicMock()


_DEFAULT_INFO = {"title": "Test", "filesize": None, "duration": 10}


def _info(**overrides) -> dict:
    return {**_DEFAULT_INFO, **overrides}


# ---------------------------------------------------------------------------
# cmd_start
# ---------------------------------------------------------------------------

class TestRateLimitInHandler:
    async def test_blocked_user_gets_wait_message(self, mock_db_and_rate_limiter):
        mock_db_and_rate_limiter.is_allowed.return_value = False
        mock_db_and_rate_limiter.seconds_until_reset.return_value = 42

        update = _make_update("https://www.tiktok.com/@user/video/123")
        context = _make_context()

        await handle_link(update, context)

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "⏱️" in text
        assert "42" in text

class TestCmdStart:
    async def test_sends_welcome_message(self):
        update = _make_update("")
        context = _make_context()

        await cmd_start(update, context)

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "👋" in text
        assert "Videito" in text
        assert "TikTok" in text
        assert "YouTube" in text


# ---------------------------------------------------------------------------
# cmd_help
# ---------------------------------------------------------------------------

class TestCmdHelp:
    async def test_lists_all_platforms(self):
        update = _make_update("")
        context = _make_context()

        await cmd_help(update, context)

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        for platform in ("TikTok", "Instagram", "Facebook", "YouTube", "Twitter"):
            assert platform in text


# ---------------------------------------------------------------------------
# handle_link — preflight check
# ---------------------------------------------------------------------------

class TestPreflight:
    async def test_non_youtube_over_limit_shows_error(self):
        update = _make_update("https://www.instagram.com/reel/abc/")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        large_info = _info(title="Big video", duration=600, filesize=150 * 1024 * 1024 + 1)
        with patch("bot.get_video_info", return_value=large_info):
            await handle_link(update, context)

        status_msg.edit_text.assert_called_once()
        assert "⚖️" in status_msg.edit_text.call_args[0][0]
        assert "150" in status_msg.edit_text.call_args[0][0]

    async def test_youtube_over_limit_shows_mp3_keyboard(self):
        update = _make_update("https://youtu.be/abc123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        large_info = _info(title="Big video", duration=600, filesize=150 * 1024 * 1024 + 1)
        with patch("bot.get_video_info", return_value=large_info):
            await handle_link(update, context)

        status_msg.edit_text.assert_called_once()
        call_kwargs = status_msg.edit_text.call_args
        text = call_kwargs[0][0]
        assert "⚖️" in text
        assert "150" in text
        assert "reply_markup" in call_kwargs.kwargs

    async def test_proceeds_when_size_unknown(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"data")

        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info(filesize=None)), \
             patch("pipeline.download_video", return_value=str(fake_video)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_link(update, context)

        status_msg.reply_video.assert_called_once()

    async def test_proceeds_when_size_within_limit(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"data")

        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info(filesize=10 * 1024 * 1024)), \
             patch("pipeline.download_video", return_value=str(fake_video)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_link(update, context)

        status_msg.reply_video.assert_called_once()


# ---------------------------------------------------------------------------
# handle_link
# ---------------------------------------------------------------------------

class TestHandleLink:
    async def test_unsupported_url_replies_with_error(self):
        update = _make_update("https://reddit.com/r/videos/comments/abc")
        context = _make_context()

        await handle_link(update, context)

        update.message.reply_text.assert_called_once()
        assert "❌" in update.message.reply_text.call_args[0][0]

    async def test_small_video_sent_as_video(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"0" * 1024)

        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", return_value=str(fake_video)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_link(update, context)

        status_msg.reply_video.assert_called_once()
        status_msg.reply_document.assert_not_called()
        status_msg.delete.assert_called_once()
        assert not fake_video.exists()

    async def test_large_video_sent_as_document_when_compression_fails(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"0" * (51 * 1024 * 1024))

        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", return_value=str(fake_video)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)), \
             patch("pipeline.compress_video", return_value=None):  # la compresión no alcanza el objetivo
            await handle_link(update, context)

        status_msg.reply_document.assert_called_once()
        status_msg.reply_video.assert_not_called()
        assert not fake_video.exists()

    async def test_private_video_error_message(self):
        update = _make_update("https://www.instagram.com/reel/abc/")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", side_effect=yt_dlp.DownloadError("ERROR: This video is private")):
            await handle_link(update, context)

        last_text = status_msg.edit_text.call_args_list[-1][0][0]
        assert "🔒" in last_text

    async def test_not_found_error_message(self):
        update = _make_update("https://www.facebook.com/watch?v=999")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", side_effect=yt_dlp.DownloadError("ERROR: 404 not found")):
            await handle_link(update, context)

        last_text = status_msg.edit_text.call_args_list[-1][0][0]
        assert "🔍" in last_text

    async def test_generic_download_error_message(self):
        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", side_effect=yt_dlp.DownloadError("ERROR: something went wrong")):
            await handle_link(update, context)

        last_text = status_msg.edit_text.call_args_list[-1][0][0]
        assert "⚠️" in last_text

    async def test_unexpected_exception_shows_generic_message(self):
        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", side_effect=RuntimeError("unexpected")):
            await handle_link(update, context)

        last_text = status_msg.edit_text.call_args_list[-1][0][0]
        assert "💥" in last_text

    async def test_temp_file_cleaned_up_on_error(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"data")

        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        status_msg.reply_video = AsyncMock(side_effect=RuntimeError("send failed"))
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", return_value=str(fake_video)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_link(update, context)

        assert not fake_video.exists()


# ---------------------------------------------------------------------------
# post_init: startup notification and duplicate detection
# ---------------------------------------------------------------------------

def _make_pending_update(update_id: int, text: str) -> MagicMock:
    upd = MagicMock()
    upd.update_id = update_id
    upd.message.text = text
    return upd


class TestPostInit:
    async def _run(self, updates, admin_id=None, notify=True, webhook_url=""):
        """
        notify: valor de NOTIFY_ON_START. Por defecto está apagado en config (para no
        spamear al admin en cada cold start de Render), así que los tests que esperan
        el aviso lo activan explícitamente.
        webhook_url: "" fuerza el modo polling, la única rama que consulta la cola de
        updates pendientes y levanta el health server.
        """
        application = MagicMock()
        application.bot.get_updates = AsyncMock(return_value=updates)
        application.bot.send_message = AsyncMock()
        application.bot.set_my_commands = AsyncMock()

        import bot
        bot._duplicate_update_ids.clear()

        with patch("bot.ADMIN_CHAT_ID", admin_id), \
             patch("bot.NOTIFY_ON_START", notify), \
             patch("bot.WEBHOOK_URL", webhook_url), \
             patch("bot.asyncio.start_server", new_callable=AsyncMock):
            await post_init(application)

        return application

    async def test_notifies_admin_on_clean_start(self):
        app = await self._run(updates=[], admin_id="999")
        app.bot.send_message.assert_called_once()
        text = app.bot.send_message.call_args.kwargs["text"]
        assert "🤖" in text
        assert "reiniciado" not in text

    async def test_notifies_admin_with_pending_count(self):
        updates = [_make_pending_update(1, "https://www.tiktok.com/@u/video/1")]
        app = await self._run(updates=updates, admin_id="999")
        text = app.bot.send_message.call_args.kwargs["text"]
        assert "1" in text
        assert "pendientes" in text

    async def test_no_notification_when_no_admin_id(self):
        app = await self._run(updates=[], admin_id=None)
        app.bot.send_message.assert_not_called()

    async def test_no_notification_when_notify_on_start_disabled(self):
        # Comportamiento por defecto: hay admin, pero NOTIFY_ON_START está apagado.
        app = await self._run(updates=[], admin_id="999", notify=False)
        app.bot.send_message.assert_not_called()

    async def test_webhook_mode_skips_update_queue_peek(self):
        # getUpdates es incompatible con un webhook activo: en esa rama no se llama.
        app = await self._run(updates=[], admin_id="999", webhook_url="https://x.dev")
        app.bot.get_updates.assert_not_called()

    async def test_detects_duplicate_urls(self):
        updates = [
            _make_pending_update(10, "https://www.tiktok.com/@u/video/abc"),
            _make_pending_update(11, "https://www.tiktok.com/@u/video/abc"),  # dup
            _make_pending_update(12, "https://www.tiktok.com/@u/video/xyz"),
            _make_pending_update(13, "https://www.tiktok.com/@u/video/abc"),  # dup
        ]
        import bot
        await self._run(updates=updates, admin_id=None)

        assert 10 not in bot._duplicate_update_ids   # first → kept
        assert 11 in bot._duplicate_update_ids
        assert 12 not in bot._duplicate_update_ids   # different URL
        assert 13 in bot._duplicate_update_ids

    async def test_duplicate_count_in_admin_message(self):
        updates = [
            _make_pending_update(1, "https://www.tiktok.com/@u/video/abc"),
            _make_pending_update(2, "https://www.tiktok.com/@u/video/abc"),
        ]
        app = await self._run(updates=updates, admin_id="999")
        text = app.bot.send_message.call_args.kwargs["text"]
        assert "🔁" in text
        assert "1" in text

    async def test_unsupported_urls_not_deduplicated(self):
        updates = [
            _make_pending_update(20, "hola cómo estás"),
            _make_pending_update(21, "hola cómo estás"),
        ]
        import bot
        await self._run(updates=updates, admin_id=None)
        assert 20 not in bot._duplicate_update_ids
        assert 21 not in bot._duplicate_update_ids


# ---------------------------------------------------------------------------
# handle_link — duplicate queue suppression
# ---------------------------------------------------------------------------

class TestDuplicateQueueSuppression:
    async def test_duplicate_update_skipped_with_message(self):
        import bot
        bot._duplicate_update_ids.add(777)

        update = _make_update("https://www.tiktok.com/@u/video/abc", update_id=777)
        await handle_link(update, _make_context())

        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "🔁" in text
        assert 777 not in bot._duplicate_update_ids  # consumido

    async def test_non_duplicate_proceeds_normally(self, tmp_path):
        import bot
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"data")

        update = _make_update("https://www.tiktok.com/@u/video/abc", update_id=888)
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", return_value=str(fake_video)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_link(update, _make_context())

        status_msg.reply_video.assert_called_once()


# ---------------------------------------------------------------------------
# handle_link — YouTube format keyboard
# ---------------------------------------------------------------------------

class TestYoutubeFormatKeyboard:
    async def test_shows_keyboard_for_youtube(self):
        update = _make_update("https://youtu.be/abc123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info(title="Test", duration=60, is_music=False)):
            await handle_link(update, context)

        status_msg.edit_text.assert_called_once()
        call_kwargs = status_msg.edit_text.call_args
        assert "reply_markup" in call_kwargs.kwargs

    async def test_shows_music_note_when_is_music(self):
        update = _make_update("https://youtu.be/abc123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info(title="Song", duration=200, is_music=True)):
            await handle_link(update, context)

        text = status_msg.edit_text.call_args[0][0]
        assert "canción" in text

    async def test_no_keyboard_for_non_youtube(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"data")

        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", return_value=str(fake_video)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_link(update, context)

        status_msg.reply_video.assert_called_once()


# ---------------------------------------------------------------------------
# handle_format_choice
# ---------------------------------------------------------------------------

class TestHandleFormatChoice:
    def _make_callback_query(self, data: str, user_id: int = 1) -> MagicMock:
        query = MagicMock()
        query.answer = AsyncMock()
        query.data = data
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        query.message = AsyncMock()
        query.message.edit_text = AsyncMock()
        query.message.reply_video = AsyncMock()
        query.message.reply_audio = AsyncMock()
        query.message.reply_document = AsyncMock()
        query.message.delete = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
        return update

    async def test_expired_pending_shows_message(self):
        update = self._make_callback_query("fmt:video", user_id=999)
        context = _make_context()

        await handle_format_choice(update, context)

        update.callback_query.edit_message_text.assert_called_once()
        assert "venció" in update.callback_query.edit_message_text.call_args[0][0]

    async def test_video_choice_sends_video(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"data")

        update = self._make_callback_query("fmt:video", user_id=42)
        context = _make_context()

        import bot
        bot._pending[42] = {"url": "https://youtu.be/abc", "status_msg": update.callback_query.message}

        with patch("pipeline.download_video", return_value=str(fake_video)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_format_choice(update, context)

        update.callback_query.message.reply_video.assert_called_once()

    async def test_audio_choice_sends_audio(self, tmp_path):
        fake_audio = tmp_path / "audio.mp3"
        fake_audio.write_bytes(b"data")

        update = self._make_callback_query("fmt:audio", user_id=43)
        context = _make_context()

        import bot
        bot._pending[43] = {"url": "https://youtu.be/abc", "status_msg": update.callback_query.message}

        with patch("bot.get_audio_info", return_value={"filesize": None}), \
             patch("pipeline.download_audio", return_value=(str(fake_audio), {"title": "Song Title", "artist": "Cool Artist"})):
            await handle_format_choice(update, context)

        update.callback_query.message.reply_audio.assert_called_once()
        call_kwargs = update.callback_query.message.reply_audio.call_args.kwargs
        assert call_kwargs["title"] == "Song Title"
        assert call_kwargs["performer"] == "Cool Artist"
        assert call_kwargs["filename"] == "Cool Artist - Song Title.mp3"

    async def test_audio_choice_filename_without_artist(self, tmp_path):
        fake_audio = tmp_path / "audio.mp3"
        fake_audio.write_bytes(b"data")

        update = self._make_callback_query("fmt:audio", user_id=44)
        context = _make_context()

        import bot
        bot._pending[44] = {"url": "https://youtu.be/abc", "status_msg": update.callback_query.message}

        with patch("bot.get_audio_info", return_value={"filesize": None}), \
             patch("pipeline.download_audio", return_value=(str(fake_audio), {"title": "Just A Title", "artist": None})):
            await handle_format_choice(update, context)

        call_kwargs = update.callback_query.message.reply_audio.call_args.kwargs
        assert call_kwargs["filename"] == "Just A Title.mp3"
        assert call_kwargs["performer"] is None

    async def test_audio_over_limit_shows_error(self):
        update = self._make_callback_query("fmt:audio", user_id=45)
        context = _make_context()

        import bot
        bot._pending[45] = {"url": "https://youtu.be/abc", "status_msg": update.callback_query.message}

        with patch("bot.get_audio_info", return_value={"filesize": 150 * 1024 * 1024 + 1}):
            await handle_format_choice(update, context)

        update.callback_query.message.reply_audio.assert_not_called()
        last_text = update.callback_query.message.edit_text.call_args_list[-1][0][0]
        assert "⚖️" in last_text
        assert "150" in last_text


# ---------------------------------------------------------------------------
# admin_only decorator
# ---------------------------------------------------------------------------

def _make_admin_update(user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    update.message.text = "/users"
    update.update_id = 0
    return update


class TestAdminOnly:
    async def test_non_admin_is_rejected(self):
        update = _make_admin_update(user_id=999)
        with patch("bot.ADMIN_CHAT_ID", "111"):
            await cmd_admin_users(update, MagicMock(args=[]))
        update.message.reply_text.assert_called_once()
        assert "⛔" in update.message.reply_text.call_args[0][0]

    async def test_admin_is_allowed(self):
        update = _make_admin_update(user_id=111)
        with patch("bot.ADMIN_CHAT_ID", "111"), \
             patch("bot.get_all_users", return_value=([], 0)):
            await cmd_admin_users(update, MagicMock(args=[]))
        # No ⛔ — reaches the handler
        text = update.message.reply_text.call_args[0][0]
        assert "⛔" not in text

    async def test_no_admin_configured_rejects_all(self):
        update = _make_admin_update(user_id=111)
        with patch("bot.ADMIN_CHAT_ID", None):
            await cmd_admin_users(update, MagicMock(args=[]))
        assert "⛔" in update.message.reply_text.call_args[0][0]


# ---------------------------------------------------------------------------
# cmd_admin_users
# ---------------------------------------------------------------------------

class TestCmdAdminUsers:
    def _make_user_row(self, uid, username, first_name, requests, last_seen="2026-04-25 10:00:00"):
        row = MagicMock()
        row.__getitem__ = lambda self, k: {
            "user_id": uid, "username": username, "first_name": first_name,
            "total_requests": requests, "last_seen": last_seen,
        }[k]
        return row

    async def test_lists_users(self):
        users = [self._make_user_row(1, "jon", "Jonatan", 42)]
        update = _make_admin_update(user_id=111)

        with patch("bot.ADMIN_CHAT_ID", "111"), \
             patch("bot.get_all_users", return_value=(users, 1)):
            await cmd_admin_users(update, MagicMock(args=[]))

        text = update.message.reply_text.call_args[0][0]
        assert "Jonatan" in text
        assert "@jon" in text
        assert "42" in text

    async def test_empty_user_list(self):
        update = _make_admin_update(user_id=111)
        with patch("bot.ADMIN_CHAT_ID", "111"), \
             patch("bot.get_all_users", return_value=([], 0)):
            await cmd_admin_users(update, MagicMock(args=[]))
        assert "No hay" in update.message.reply_text.call_args[0][0]

    async def test_pagination_hint_shown(self):
        users = [self._make_user_row(i, None, f"User{i}", i) for i in range(20)]
        update = _make_admin_update(user_id=111)
        with patch("bot.ADMIN_CHAT_ID", "111"), \
             patch("bot.get_all_users", return_value=(users, 45)):
            await cmd_admin_users(update, MagicMock(args=[]))
        text = update.message.reply_text.call_args[0][0]
        assert "Página 1/3" in text
        assert "/users 2" in text

    async def test_page_arg_forwarded_to_db(self):
        update = _make_admin_update(user_id=111)
        with patch("bot.ADMIN_CHAT_ID", "111"), \
             patch("bot.get_all_users", return_value=([], 0)) as mock_db:
            await cmd_admin_users(update, MagicMock(args=["3"]))
        mock_db.assert_called_once_with(page=3, page_size=20)


# ---------------------------------------------------------------------------
# cmd_admin_stats
# ---------------------------------------------------------------------------

class TestCmdAdminStats:
    async def test_shows_stats(self):
        update = _make_admin_update(user_id=111)
        with patch("bot.ADMIN_CHAT_ID", "111"), \
             patch("bot.get_stats", return_value={"total_users": 5, "total_requests": 99}):
            await cmd_admin_stats(update, MagicMock())
        text = update.message.reply_text.call_args[0][0]
        assert "5" in text
        assert "99" in text
        assert "📊" in text


# ---------------------------------------------------------------------------
# Nuevas features: multi-link, carrusel, botón MP3
# ---------------------------------------------------------------------------

class TestMultiLink:
    async def test_processes_each_link(self, tmp_path, mock_db_and_rate_limiter):
        f1 = tmp_path / "a.mp4"; f1.write_bytes(b"d")
        f2 = tmp_path / "b.mp4"; f2.write_bytes(b"d")
        text = "https://www.tiktok.com/@u/video/1 https://www.tiktok.com/@u/video/2"
        update = _make_update(text)
        update.message.reply_text = AsyncMock(return_value=AsyncMock())
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", side_effect=[str(f1), str(f2)]), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_link(update, context)

        # dos links → dos verificaciones de rate limit y dos status messages
        assert mock_db_and_rate_limiter.is_allowed.call_count == 2
        assert update.message.reply_text.call_count == 2

    async def test_partial_when_rate_limited(self, tmp_path, mock_db_and_rate_limiter):
        f1 = tmp_path / "a.mp4"; f1.write_bytes(b"d")
        mock_db_and_rate_limiter.is_allowed.side_effect = [True, False]
        text = "https://www.tiktok.com/@u/video/1 https://www.tiktok.com/@u/video/2"
        update = _make_update(text)
        update.message.reply_text = AsyncMock(return_value=AsyncMock())
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", return_value=str(f1)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_link(update, context)

        texts = [c[0][0] for c in update.message.reply_text.call_args_list]
        assert any("1 de 2" in t for t in texts)


class TestCarousel:
    async def test_sends_album_and_cleans_up(self, tmp_path):
        f1 = tmp_path / "1.mp4"; f1.write_bytes(b"v")
        f2 = tmp_path / "2.jpg"; f2.write_bytes(b"i")
        items = [{"path": str(f1), "kind": "video"}, {"path": str(f2), "kind": "photo"}]
        update = _make_update("https://www.instagram.com/p/abc/")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info(is_playlist=True, count=2)), \
             patch("pipeline.download_post", return_value=items):
            await handle_link(update, context)

        status_msg.reply_media_group.assert_called_once()
        media = status_msg.reply_media_group.call_args.kwargs["media"]
        assert len(media) == 2
        # archivos limpiados tras enviar
        assert not os.path.exists(str(f1))
        assert not os.path.exists(str(f2))


class TestSongButton:
    async def test_button_shown_when_song_identified(self, tmp_path):
        fake = tmp_path / "v.mp4"; fake.write_bytes(b"d")
        update = _make_update("https://www.tiktok.com/@u/video/1")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info(song={"track": "Flowers", "artist": "Miley Cyrus"})), \
             patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_link(update, context)

        kb = status_msg.reply_video.call_args.kwargs["reply_markup"]
        btn = kb.inline_keyboard[0][0]
        assert btn.callback_data.startswith("song:")
        assert "Miley Cyrus" in btn.text and "Flowers" in btn.text

    async def test_no_button_when_no_song(self, tmp_path):
        fake = tmp_path / "v.mp4"; fake.write_bytes(b"d")
        update = _make_update("https://www.tiktok.com/@u/video/1")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info(song=None)), \
             patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await handle_link(update, context)

        assert status_msg.reply_video.call_args.kwargs["reply_markup"] is None


class TestSongDownload:
    def _make_cbq(self, data):
        update = MagicMock()
        update.callback_query.data = data
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_reply_markup = AsyncMock()
        update.callback_query.message = AsyncMock()
        return update

    async def test_expired_token_alerts_without_download(self):
        update = self._make_cbq("song:doesnotexist")
        await handle_song_download(update, _make_context())
        update.callback_query.message.reply_text.assert_not_called()
        # segundo answer es la alerta de expiración
        assert update.callback_query.answer.call_count == 2

    async def test_valid_token_downloads_song(self, tmp_path):
        import bot
        fake = tmp_path / "song.mp3"; fake.write_bytes(b"d")
        token = bot._store_song("Miley Cyrus Flowers")
        update = self._make_cbq(f"song:{token}")
        status_msg = AsyncMock()
        update.callback_query.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("pipeline.download_song", return_value=(str(fake), {"title": "Flowers", "artist": "Miley Cyrus"})):
            await handle_song_download(update, context)

        status_msg.reply_audio.assert_called_once()
        assert not os.path.exists(str(fake))


class TestCompressionInDownload:
    async def test_large_video_compressed_and_sent_as_video(self, tmp_path):
        big = tmp_path / "v.mp4"; big.write_bytes(b"x")
        comp = tmp_path / "v_compressed.mp4"; comp.write_bytes(b"y")
        update = _make_update("https://www.tiktok.com/@u/video/1")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", return_value=str(big)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)), \
             patch("pipeline.os.path.getsize", return_value=60 * 1024 * 1024), \
             patch("pipeline.compress_video", return_value=str(comp)):
            await handle_link(update, context)

        status_msg.reply_video.assert_called_once()
        status_msg.reply_document.assert_not_called()

    async def test_falls_back_to_document_when_compression_fails(self, tmp_path):
        big = tmp_path / "v.mp4"; big.write_bytes(b"x")
        update = _make_update("https://www.tiktok.com/@u/video/1")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.get_video_info", return_value=_info()), \
             patch("pipeline.download_video", return_value=str(big)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)), \
             patch("pipeline.os.path.getsize", return_value=60 * 1024 * 1024), \
             patch("pipeline.compress_video", return_value=None):
            await handle_link(update, context)

        status_msg.reply_document.assert_called_once()
        status_msg.reply_video.assert_not_called()
