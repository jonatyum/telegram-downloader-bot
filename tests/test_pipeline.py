"""
Tests de pipeline.py contra un Messenger falso, sin tocar Telegram. Las funciones del
motor (download_video, compress_video, ...) se parchean por nombre en el namespace de
pipeline.py, dejando que asyncio.get_running_loop()/run_in_executor corran de verdad
sobre el thread pool real: es más robusto que simular la cola de run_in_executor, porque
prueba el código de verdad en lugar de un mecanismo genérico de despacho.
"""
import asyncio
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp

from pipeline import (
    DeliveryLimits,
    Pipeline,
    _audio_filename,
    _progress_bridge,
    _quality_note,
    download_error_message,
)


class FakeMessenger:
    """Registra cada llamada del pipeline para poder aseverar sobre ellas."""

    def __init__(self, limits: DeliveryLimits):
        self.limits = limits
        self.updates: list[str] = []
        self.finished = False
        self.sent: list[tuple] = []  # (kind, path, kwargs)

    async def update(self, text: str) -> None:
        self.updates.append(text)

    async def finish(self) -> None:
        self.finished = True

    async def send_video(self, path, *, width, height, caption, song):
        self.sent.append(("video", path, {"width": width, "height": height, "caption": caption, "song": song}))

    async def send_audio(self, path, *, title, performer, filename):
        self.sent.append(("audio", path, {"title": title, "performer": performer, "filename": filename}))

    async def send_photo(self, path):
        self.sent.append(("photo", path, {}))

    async def send_document(self, path, *, caption, song):
        self.sent.append(("document", path, {"caption": caption, "song": song}))

    async def send_album(self, items):
        self.sent.append(("album", None, {"items": items}))


DEFAULT_LIMITS = DeliveryLimits(max_inline_bytes=50 * 1024 * 1024, max_compress_height=720)


def _pipeline(max_concurrent=5, default_height=1080) -> Pipeline:
    return Pipeline(max_concurrent, default_height)


# ---------------------------------------------------------------------------
# _progress_bridge
# ---------------------------------------------------------------------------

class TestProgressBridge:
    def _make(self):
        loop = MagicMock()
        messenger = FakeMessenger(DEFAULT_LIMITS)
        cb = _progress_bridge(loop, messenger)
        return cb, loop, messenger

    def test_downloading_status_triggers_update(self):
        cb, loop, _ = self._make()
        with patch("pipeline.asyncio.run_coroutine_threadsafe") as mock_rct:
            cb("downloading")
        assert mock_rct.call_count == 1

    def test_finished_status_triggers_update(self):
        cb, loop, _ = self._make()
        with patch("pipeline.asyncio.run_coroutine_threadsafe") as mock_rct:
            cb("downloading")
            cb("finished")
        assert mock_rct.call_count == 2

    def test_same_status_repeated_does_not_trigger(self):
        cb, loop, _ = self._make()
        with patch("pipeline.asyncio.run_coroutine_threadsafe") as mock_rct:
            cb("downloading")
            cb("downloading")
            cb("downloading")
        assert mock_rct.call_count == 1

    def test_unknown_status_ignored(self):
        cb, loop, _ = self._make()
        with patch("pipeline.asyncio.run_coroutine_threadsafe") as mock_rct:
            cb("error")
            cb("processing")
            cb("")
        assert mock_rct.call_count == 0

    async def test_report_swallows_messenger_errors(self):
        """El try/except es específico de esta vía fire-and-forget: un fallo en
        messenger.update() no debe propagar hacia el hilo del hook de yt-dlp."""
        messenger = FakeMessenger(DEFAULT_LIMITS)

        async def _boom(text):
            raise RuntimeError("edit falló")

        messenger.update = _boom
        loop = asyncio.get_running_loop()
        cb = _progress_bridge(loop, messenger)
        cb("downloading")
        # Deja que el coroutine agendado por run_coroutine_threadsafe corra.
        await asyncio.sleep(0.05)
        # Si no se hubiera atrapado, quedaría un "exception never retrieved"; el test
        # simplemente no debe fallar ni colgarse.


# ---------------------------------------------------------------------------
# helpers puros
# ---------------------------------------------------------------------------

class TestAudioFilename:
    def test_with_artist(self):
        assert _audio_filename("Flowers", "Miley Cyrus") == "Miley Cyrus - Flowers.mp3"

    def test_without_artist(self):
        assert _audio_filename("Just A Title", None) == "Just A Title.mp3"


class TestQualityNote:
    def test_none_when_no_preference(self):
        assert _quality_note(None, 1080) is None

    def test_none_when_height_zero(self):
        assert _quality_note(720, 0) is None

    def test_lower_than_preference(self):
        note = _quality_note(1080, 480)
        assert "480p" in note and "1080p" in note

    def test_higher_than_preference(self):
        note = _quality_note(480, 1080)
        assert "1080p" in note and "480p" in note

    def test_none_when_matches_preference(self):
        assert _quality_note(720, 720) is None


class TestDownloadErrorMessage:
    def test_private_or_login(self):
        assert "🔒" in download_error_message("ERROR: This video is private")
        assert "🔒" in download_error_message("ERROR: login required")

    def test_not_found(self):
        assert "🔍" in download_error_message("ERROR: 404 not found")

    def test_extraction_failure(self):
        assert "🛠️" in download_error_message("Unable to extract webpage (rehydration failed)")

    def test_generic_fallback(self):
        assert "⚠️" in download_error_message("something completely unexpected")


# ---------------------------------------------------------------------------
# Pipeline.download — video
# ---------------------------------------------------------------------------

class TestPipelineDownloadVideo:
    async def test_small_video_sent_as_video(self, tmp_path):
        fake = tmp_path / "video.mp4"
        fake.write_bytes(b"0" * 1024)
        messenger = FakeMessenger(DEFAULT_LIMITS)

        with patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(1280, 720)):
            await _pipeline().download("https://x", "video", messenger=messenger)

        assert messenger.sent == [("video", str(fake), {"width": 1280, "height": 720, "caption": None, "song": None})]
        assert messenger.finished is True
        assert not fake.exists()  # limpiado
        assert any("⏳" in t for t in messenger.updates)
        assert any("⬇️" in t for t in messenger.updates)

    async def test_large_video_compressed_and_sent_as_video(self, tmp_path):
        big = tmp_path / "v.mp4"; big.write_bytes(b"x" * 100)
        comp = tmp_path / "v_compressed.mp4"; comp.write_bytes(b"y" * 10)
        messenger = FakeMessenger(DeliveryLimits(max_inline_bytes=50, max_compress_height=720))

        with patch("pipeline.download_video", return_value=str(big)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)), \
             patch("pipeline.compress_video", return_value=str(comp)) as mock_compress:
            await _pipeline().download("https://x", "video", messenger=messenger)

        assert messenger.sent[0][0] == "video"
        assert messenger.sent[0][1] == str(comp)
        mock_compress.assert_called_once_with(str(big), 50, 720)
        # Ambos temporales (original grande y comprimido) se limpian.
        assert not big.exists()
        assert not comp.exists()

    async def test_falls_back_to_document_when_compression_fails(self, tmp_path):
        big = tmp_path / "v.mp4"; big.write_bytes(b"x" * 100)
        messenger = FakeMessenger(DeliveryLimits(max_inline_bytes=50, max_compress_height=720))

        with patch("pipeline.download_video", return_value=str(big)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)), \
             patch("pipeline.compress_video", return_value=None):
            await _pipeline().download("https://x", "video", messenger=messenger)

        assert messenger.sent[0][0] == "document"
        assert messenger.sent[0][1] == str(big)
        assert not big.exists()

    async def test_photo_sent_when_result_is_image(self, tmp_path):
        img = tmp_path / "photo.jpg"; img.write_bytes(b"i")
        messenger = FakeMessenger(DEFAULT_LIMITS)

        with patch("pipeline.download_video", return_value=str(img)):
            await _pipeline().download("https://x", "video", messenger=messenger)

        assert messenger.sent == [("photo", str(img), {})]
        assert messenger.finished is True
        assert not img.exists()

    async def test_quality_note_included_when_below_preference(self, tmp_path):
        fake = tmp_path / "video.mp4"; fake.write_bytes(b"0")
        messenger = FakeMessenger(DEFAULT_LIMITS)

        with patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(640, 480)):
            await _pipeline().download("https://x", "video", messenger=messenger, user_pref_height=1080)

        caption = messenger.sent[0][2]["caption"]
        assert "480p" in caption and "1080p" in caption

    async def test_song_passed_through_to_send_video(self, tmp_path):
        fake = tmp_path / "video.mp4"; fake.write_bytes(b"0")
        messenger = FakeMessenger(DEFAULT_LIMITS)
        song = {"track": "Flowers", "artist": "Miley Cyrus"}

        with patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await _pipeline().download("https://x", "video", messenger=messenger, song=song)

        assert messenger.sent[0][2]["song"] == song

    async def test_effective_height_uses_preference_over_default(self, tmp_path):
        fake = tmp_path / "video.mp4"; fake.write_bytes(b"0")
        messenger = FakeMessenger(DEFAULT_LIMITS)

        with patch("pipeline.download_video", return_value=str(fake)) as mock_dl, \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await _pipeline(default_height=1080).download(
                "https://x", "video", messenger=messenger, user_pref_height=480,
            )

        # download_video(url, progress_cb, effective_height) — el 3er posicional es la altura.
        assert mock_dl.call_args[0][2] == 480

    async def test_temp_file_cleaned_up_when_send_fails(self, tmp_path):
        fake = tmp_path / "video.mp4"; fake.write_bytes(b"0")
        messenger = FakeMessenger(DEFAULT_LIMITS)

        async def _boom(*a, **k):
            raise RuntimeError("send failed")

        messenger.send_video = _boom

        with patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            with pytest.raises(RuntimeError):
                await _pipeline().download("https://x", "video", messenger=messenger)

        assert not fake.exists()


# ---------------------------------------------------------------------------
# Pipeline.download — audio
# ---------------------------------------------------------------------------

class TestPipelineDownloadAudio:
    async def test_audio_sent_with_computed_filename(self, tmp_path):
        fake = tmp_path / "audio.mp3"; fake.write_bytes(b"d")
        messenger = FakeMessenger(DEFAULT_LIMITS)

        with patch("pipeline.download_audio", return_value=(str(fake), {"title": "Song", "artist": "Artist"})):
            await _pipeline().download("https://x", "audio", messenger=messenger)

        assert messenger.sent == [("audio", str(fake), {"title": "Song", "performer": "Artist", "filename": "Artist - Song.mp3"})]
        assert not fake.exists()

    async def test_audio_filename_without_artist(self, tmp_path):
        fake = tmp_path / "audio.mp3"; fake.write_bytes(b"d")
        messenger = FakeMessenger(DEFAULT_LIMITS)

        with patch("pipeline.download_audio", return_value=(str(fake), {"title": "Solo Title", "artist": None})):
            await _pipeline().download("https://x", "audio", messenger=messenger)

        assert messenger.sent[0][2]["filename"] == "Solo Title.mp3"
        assert messenger.sent[0][2]["performer"] is None


# ---------------------------------------------------------------------------
# Pipeline.download — concurrencia
# ---------------------------------------------------------------------------

class TestPipelineConcurrency:
    async def test_shows_queue_message_before_download(self, tmp_path):
        fake = tmp_path / "video.mp4"; fake.write_bytes(b"0")
        messenger = FakeMessenger(DEFAULT_LIMITS)

        with patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            await _pipeline().download("https://x", "video", messenger=messenger)

        assert any("⏳" in t for t in messenger.updates)

    async def test_blocks_when_semaphore_full(self, tmp_path):
        fake = tmp_path / "video.mp4"; fake.write_bytes(b"0")
        messenger = FakeMessenger(DEFAULT_LIMITS)
        p = _pipeline(max_concurrent=0)  # todos los slots ocupados

        with patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            task = asyncio.create_task(p.download("https://x", "video", messenger=messenger))
            for _ in range(10):
                await asyncio.sleep(0)

            assert not task.done()
            assert any("⏳" in t for t in messenger.updates)
            # Todavía no llegó a "descargando": está esperando el semáforo.
            assert not any("⬇️" in t for t in messenger.updates)

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Pipeline.carousel
# ---------------------------------------------------------------------------

class TestPipelineCarousel:
    async def test_sends_album_and_cleans_up(self, tmp_path):
        f1 = tmp_path / "1.mp4"; f1.write_bytes(b"v")
        f2 = tmp_path / "2.jpg"; f2.write_bytes(b"i")
        items = [{"path": str(f1), "kind": "video"}, {"path": str(f2), "kind": "photo"}]
        messenger = FakeMessenger(DEFAULT_LIMITS)

        with patch("pipeline.download_post", return_value=items):
            await _pipeline().carousel("https://x", messenger=messenger)

        assert messenger.sent == [("album", None, {"items": items})]
        assert messenger.finished is True
        assert not f1.exists() and not f2.exists()

    async def test_empty_items_shows_warning_without_sending(self):
        messenger = FakeMessenger(DEFAULT_LIMITS)

        with patch("pipeline.download_post", return_value=[]):
            await _pipeline().carousel("https://x", messenger=messenger)

        assert messenger.sent == []
        assert messenger.finished is False
        assert any("⚠️" in t for t in messenger.updates)


# ---------------------------------------------------------------------------
# Pipeline.song
# ---------------------------------------------------------------------------

class TestPipelineSong:
    async def test_downloads_and_sends_audio(self, tmp_path):
        fake = tmp_path / "song.mp3"; fake.write_bytes(b"d")
        messenger = FakeMessenger(DEFAULT_LIMITS)

        with patch("pipeline.download_song", return_value=(str(fake), {"title": "Flowers", "artist": "Miley Cyrus"})):
            await _pipeline().song("Miley Cyrus Flowers", messenger=messenger)

        assert messenger.sent == [("audio", str(fake), {"title": "Flowers", "performer": "Miley Cyrus", "filename": "Miley Cyrus - Flowers.mp3"})]
        assert not fake.exists()

    async def test_cleans_up_on_error(self, tmp_path):
        messenger = FakeMessenger(DEFAULT_LIMITS)

        def _raise(*a, **k):
            raise yt_dlp.DownloadError("sin resultados")

        with patch("pipeline.download_song", side_effect=_raise):
            with pytest.raises(yt_dlp.DownloadError):
                await _pipeline().song("query rara", messenger=messenger)

        assert messenger.sent == []
