import asyncio
import os
import pytest
import yt_dlp
from unittest.mock import AsyncMock, MagicMock, patch

from bot import _is_supported_url, _is_youtube_url, cmd_start, cmd_help, handle_link, handle_format_choice, _make_progress_callback


@pytest.fixture(autouse=True)
def mock_db_and_rate_limiter():
    with patch("bot.upsert_user"), \
         patch("bot.rate_limiter") as mock_rl:
        mock_rl.is_allowed.return_value = True
        mock_rl.seconds_until_reset.return_value = 30
        yield mock_rl


# ---------------------------------------------------------------------------
# _is_supported_url
# ---------------------------------------------------------------------------

class TestIsSupportedUrl:
    @pytest.mark.parametrize("url", [
        "https://www.tiktok.com/@user/video/123",
        "https://vm.tiktok.com/abc123/",
        "https://www.instagram.com/reel/abc123/",
        "https://instagr.am/p/abc/",
        "https://www.facebook.com/watch?v=123",
        "https://fb.watch/abc123/",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/shorts/abc123",
        "https://twitter.com/user/status/123456/video/1",
        "https://x.com/user/status/123456",
        "https://t.co/abc123",
    ])
    def test_supported_urls(self, url):
        assert _is_supported_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://reddit.com/r/videos/",
        "https://example.com/video.mp4",
        "not a url at all",
        "",
        "ftp://tiktok.com/video",
    ])
    def test_unsupported_urls(self, url):
        assert _is_supported_url(url) is False

    def test_no_subdomain_spoofing(self):
        # "notiktok.com" should not match "tiktok.com"
        assert _is_supported_url("https://notiktok.com/video") is False
        assert _is_supported_url("https://faketiktok.com/video") is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(text: str) -> MagicMock:
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.message.reply_video = AsyncMock()
    update.message.reply_document = AsyncMock()
    return update


def _make_context() -> MagicMock:
    return MagicMock()


_DEFAULT_INFO = {"title": "Test", "filesize": None, "duration": 10}


def _mock_executor(download_result=None, download_error=None, info=None):
    """Devuelve un AsyncMock que simula las dos llamadas a run_in_executor:
    1ª llamada (preflight get_video_info) → info dict
    2ª llamada (download_video)           → download_result o lanza download_error
    """
    preflight = info or _DEFAULT_INFO

    async def _side_effect(executor, func, *args):
        if not hasattr(_side_effect, "_called"):
            _side_effect._called = True
            return preflight
        if download_error:
            raise download_error
        return download_result

    return AsyncMock(side_effect=_side_effect)


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
        assert "TikTok" in text
        assert "X/Twitter" in text


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
    async def test_rejects_video_over_limit(self):
        update = _make_update("https://youtu.be/abc123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        large_info = {"title": "Big video", "duration": 600, "filesize": 150 * 1024 * 1024}
        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(info=large_info)
            await handle_link(update, context)

        status_msg.edit_text.assert_called_once()
        assert "❌" in status_msg.edit_text.call_args[0][0]
        assert "150" in status_msg.edit_text.call_args[0][0]

    async def test_proceeds_when_size_unknown(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"data")

        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(
                info={"title": "Video", "duration": 60, "filesize": None},
                download_result=str(fake_video),
            )
            await handle_link(update, context)

        status_msg.reply_video.assert_called_once()

    async def test_proceeds_when_size_within_limit(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"data")

        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(
                info={"title": "Video", "duration": 60, "filesize": 10 * 1024 * 1024},
                download_result=str(fake_video),
            )
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

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(download_result=str(fake_video))
            await handle_link(update, context)

        status_msg.reply_video.assert_called_once()
        status_msg.reply_document.assert_not_called()
        status_msg.delete.assert_called_once()
        assert not fake_video.exists()

    async def test_large_video_sent_as_document(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"0" * (51 * 1024 * 1024))

        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(download_result=str(fake_video))
            await handle_link(update, context)

        status_msg.reply_document.assert_called_once()
        status_msg.reply_video.assert_not_called()
        assert not fake_video.exists()

    async def test_private_video_error_message(self):
        update = _make_update("https://www.instagram.com/reel/abc/")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(
                download_error=yt_dlp.DownloadError("ERROR: This video is private")
            )
            await handle_link(update, context)

        last_text = status_msg.edit_text.call_args_list[-1][0][0]
        assert "🔒" in last_text

    async def test_not_found_error_message(self):
        update = _make_update("https://www.facebook.com/watch?v=999")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(
                download_error=yt_dlp.DownloadError("ERROR: 404 not found")
            )
            await handle_link(update, context)

        last_text = status_msg.edit_text.call_args_list[-1][0][0]
        assert "🔍" in last_text

    async def test_generic_download_error_message(self):
        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(
                download_error=yt_dlp.DownloadError("ERROR: something went wrong")
            )
            await handle_link(update, context)

        last_text = status_msg.edit_text.call_args_list[-1][0][0]
        assert "⚠️" in last_text

    async def test_unexpected_exception_shows_generic_message(self):
        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(
                download_error=RuntimeError("unexpected")
            )
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

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(download_result=str(fake_video))
            await handle_link(update, context)

        assert not fake_video.exists()


# ---------------------------------------------------------------------------
# _make_progress_callback
# ---------------------------------------------------------------------------

class TestMakeProgressCallback:
    def _make_cb(self):
        loop = MagicMock()
        status_msg = AsyncMock()
        cb = _make_progress_callback(loop, status_msg)
        return cb, loop, status_msg

    def test_downloading_status_triggers_update(self):
        cb, loop, _ = self._make_cb()
        with patch("bot.asyncio.run_coroutine_threadsafe") as mock_rct:
            cb("downloading")
        assert mock_rct.call_count == 1

    def test_finished_status_triggers_update(self):
        cb, loop, _ = self._make_cb()
        with patch("bot.asyncio.run_coroutine_threadsafe") as mock_rct:
            cb("downloading")
            cb("finished")
        assert mock_rct.call_count == 2

    def test_same_status_repeated_does_not_trigger(self):
        cb, loop, _ = self._make_cb()
        with patch("bot.asyncio.run_coroutine_threadsafe") as mock_rct:
            cb("downloading")
            cb("downloading")
            cb("downloading")
        assert mock_rct.call_count == 1

    def test_unknown_status_ignored(self):
        cb, loop, _ = self._make_cb()
        with patch("bot.asyncio.run_coroutine_threadsafe") as mock_rct:
            cb("error")
            cb("processing")
            cb("")
        assert mock_rct.call_count == 0


# ---------------------------------------------------------------------------
# _is_youtube_url
# ---------------------------------------------------------------------------

class TestIsYoutubeUrl:
    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc123",
        "https://youtube.com/shorts/abc",
        "https://music.youtube.com/watch?v=abc",
    ])
    def test_youtube_urls_detected(self, url):
        assert _is_youtube_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://www.tiktok.com/@user/video/123",
        "https://www.instagram.com/reel/abc/",
        "https://x.com/user/status/123",
    ])
    def test_non_youtube_urls_rejected(self, url):
        assert _is_youtube_url(url) is False


# ---------------------------------------------------------------------------
# handle_link — YouTube format keyboard
# ---------------------------------------------------------------------------

class TestYoutubeFormatKeyboard:
    async def test_shows_keyboard_for_youtube(self):
        update = _make_update("https://youtu.be/abc123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(
                info={"title": "Test", "filesize": None, "duration": 60, "is_music": False}
            )
            await handle_link(update, context)

        status_msg.edit_text.assert_called_once()
        call_kwargs = status_msg.edit_text.call_args
        assert "reply_markup" in call_kwargs.kwargs

    async def test_shows_music_note_when_is_music(self):
        update = _make_update("https://youtu.be/abc123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(
                info={"title": "Song", "filesize": None, "duration": 200, "is_music": True}
            )
            await handle_link(update, context)

        text = status_msg.edit_text.call_args[0][0]
        assert "🎵" in text

    async def test_no_keyboard_for_non_youtube(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"data")

        update = _make_update("https://www.tiktok.com/@user/video/123")
        status_msg = AsyncMock()
        update.message.reply_text = AsyncMock(return_value=status_msg)
        context = _make_context()

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = _mock_executor(download_result=str(fake_video))
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
        assert "expiró" in update.callback_query.edit_message_text.call_args[0][0]

    async def test_video_choice_sends_video(self, tmp_path):
        fake_video = tmp_path / "video.mp4"
        fake_video.write_bytes(b"data")

        update = self._make_callback_query("fmt:video", user_id=42)
        context = _make_context()

        import bot
        bot._pending[42] = {"url": "https://youtu.be/abc", "status_msg": update.callback_query.message}

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = AsyncMock(return_value=str(fake_video))
            await handle_format_choice(update, context)

        update.callback_query.message.reply_video.assert_called_once()

    async def test_audio_choice_sends_audio(self, tmp_path):
        fake_audio = tmp_path / "audio.mp3"
        fake_audio.write_bytes(b"data")

        update = self._make_callback_query("fmt:audio", user_id=43)
        context = _make_context()

        import bot
        bot._pending[43] = {"url": "https://youtu.be/abc", "status_msg": update.callback_query.message}

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = AsyncMock(
                return_value=(str(fake_audio), {"title": "Song Title", "artist": "Cool Artist"})
            )
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

        with patch("bot.asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.run_in_executor = AsyncMock(
                return_value=(str(fake_audio), {"title": "Just A Title", "artist": None})
            )
            await handle_format_choice(update, context)

        call_kwargs = update.callback_query.message.reply_audio.call_args.kwargs
        assert call_kwargs["filename"] == "Just A Title.mp3"
        assert call_kwargs["performer"] is None
