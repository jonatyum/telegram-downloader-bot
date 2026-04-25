import json
import os
import pytest
import yt_dlp
from unittest.mock import MagicMock, patch, call

from downloader import download_video, download_audio, _make_output_path, get_video_dimensions, get_video_info, get_audio_info, _estimate_filesize
from config import DOWNLOAD_DIR


class TestMakeOutputPath:
    def test_creates_download_dir(self, tmp_path):
        target = str(tmp_path / "subdir")
        with patch("downloader.DOWNLOAD_DIR", target):
            path = _make_output_path()
        assert os.path.isdir(target)

    def test_path_contains_uuid_and_ext_placeholder(self, tmp_path):
        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)):
            path = _make_output_path()
        assert "%(ext)s" in path
        assert str(tmp_path) in path

    def test_each_call_returns_unique_path(self, tmp_path):
        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)):
            p1 = _make_output_path()
            p2 = _make_output_path()
        assert p1 != p2


class TestDownloadVideo:
    def _mock_ydl(self, filename: str) -> MagicMock:
        ydl = MagicMock()
        ydl.extract_info.return_value = {"id": "abc", "ext": "mp4"}
        ydl.prepare_filename.return_value = filename
        return ydl

    def test_returns_filepath_when_file_exists(self, tmp_path):
        fake_file = tmp_path / "video.mp4"
        fake_file.write_bytes(b"data")

        ydl_mock = self._mock_ydl(str(fake_file))
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl_mock)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", return_value=cm):
            result = download_video("https://www.tiktok.com/@user/video/123")

        assert result == str(fake_file)

    def test_falls_back_to_mp4_when_original_missing(self, tmp_path):
        reported_path = str(tmp_path / "video.webm")
        fallback_path = str(tmp_path / "video.mp4")

        # Only the .mp4 fallback exists on disk
        open(fallback_path, "wb").close()

        ydl_mock = self._mock_ydl(reported_path)
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl_mock)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", return_value=cm):
            result = download_video("https://www.tiktok.com/@user/video/123")

        assert result == fallback_path

    def test_propagates_download_error(self, tmp_path):
        ydl_mock = MagicMock()
        ydl_mock.extract_info.side_effect = yt_dlp.DownloadError("ERROR: 404")
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl_mock)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", return_value=cm):
            with pytest.raises(yt_dlp.DownloadError):
                download_video("https://www.tiktok.com/@user/video/bad")

    def test_passes_correct_options_to_ydl(self, tmp_path):
        fake_file = tmp_path / "video.mp4"
        fake_file.write_bytes(b"data")

        ydl_mock = self._mock_ydl(str(fake_file))
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl_mock)
        cm.__exit__ = MagicMock(return_value=False)

        captured_opts = {}

        def capture_ydl(opts):
            captured_opts.update(opts)
            return cm

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", side_effect=capture_ydl):
            download_video("https://www.tiktok.com/@user/video/123")

        assert captured_opts["quiet"] is True
        assert captured_opts["merge_output_format"] == "mp4"
        assert captured_opts["retries"] == 3
        assert "max_filesize" in captured_opts
        assert "progress_hooks" in captured_opts
        assert "720" in captured_opts["format"]

    def test_progress_callback_called_on_download(self, tmp_path):
        fake_file = tmp_path / "video.mp4"
        fake_file.write_bytes(b"data")

        progress_calls = []

        def fake_on_progress(d):
            progress_calls.append(d)

        ydl_mock = self._mock_ydl(str(fake_file))
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl_mock)
        cm.__exit__ = MagicMock(return_value=False)

        captured_opts = {}

        def capture_ydl(opts):
            captured_opts.update(opts)
            return cm

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", side_effect=capture_ydl):
            download_video("https://www.tiktok.com/@user/video/123", on_progress=fake_on_progress)

        # Simulate the hook firing — now receives status string
        hook = captured_opts["progress_hooks"][0]
        hook({"status": "downloading"})
        assert progress_calls == ["downloading"]

    def test_progress_hook_passes_all_statuses(self, tmp_path):
        fake_file = tmp_path / "video.mp4"
        fake_file.write_bytes(b"data")

        progress_calls = []
        ydl_mock = self._mock_ydl(str(fake_file))
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl_mock)
        cm.__exit__ = MagicMock(return_value=False)

        captured_opts = {}

        def capture_ydl(opts):
            captured_opts.update(opts)
            return cm

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", side_effect=capture_ydl):
            download_video("https://www.tiktok.com/@user/video/123", on_progress=lambda s: progress_calls.append(s))

        hook = captured_opts["progress_hooks"][0]
        hook({"status": "downloading"})
        hook({"status": "finished"})
        assert progress_calls == ["downloading", "finished"]


class TestEstimateFilesize:
    def test_uses_filesize_when_available(self):
        info = {"filesize": 1000, "filesize_approx": 500}
        assert _estimate_filesize(info) == 1000

    def test_falls_back_to_approx(self):
        info = {"filesize": None, "filesize_approx": 500}
        assert _estimate_filesize(info) == 500

    def test_sums_dash_streams(self):
        info = {
            "requested_formats": [
                {"filesize": 800, "filesize_approx": None},
                {"filesize": 200, "filesize_approx": None},
            ]
        }
        assert _estimate_filesize(info) == 1000

    def test_returns_none_when_no_size(self):
        assert _estimate_filesize({}) is None


class TestGetAudioInfo:
    def _mock_ydl_cm(self, info: dict) -> MagicMock:
        ydl = MagicMock()
        ydl.extract_info.return_value = info
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    def test_returns_filesize(self):
        cm = self._mock_ydl_cm({"filesize": 8 * 1024 * 1024})
        with patch("yt_dlp.YoutubeDL", return_value=cm):
            result = get_audio_info("https://youtu.be/abc")
        assert result["filesize"] == 8 * 1024 * 1024

    def test_falls_back_to_filesize_approx(self):
        cm = self._mock_ydl_cm({"filesize": None, "filesize_approx": 5 * 1024 * 1024})
        with patch("yt_dlp.YoutubeDL", return_value=cm):
            result = get_audio_info("https://youtu.be/abc")
        assert result["filesize"] == 5 * 1024 * 1024

    def test_returns_none_when_no_size(self):
        cm = self._mock_ydl_cm({})
        with patch("yt_dlp.YoutubeDL", return_value=cm):
            result = get_audio_info("https://youtu.be/abc")
        assert result["filesize"] is None

    def test_propagates_download_error(self):
        ydl = MagicMock()
        ydl.extract_info.side_effect = yt_dlp.DownloadError("private")
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl)
        cm.__exit__ = MagicMock(return_value=False)
        with patch("yt_dlp.YoutubeDL", return_value=cm):
            with pytest.raises(yt_dlp.DownloadError):
                get_audio_info("https://youtu.be/bad")


class TestGetVideoInfo:
    def _mock_ydl_cm(self, info: dict) -> MagicMock:
        ydl = MagicMock()
        ydl.extract_info.return_value = info
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    def test_returns_title_duration_filesize(self):
        cm = self._mock_ydl_cm({
            "title": "Test video",
            "duration": 120,
            "filesize": 5 * 1024 * 1024,
        })
        with patch("yt_dlp.YoutubeDL", return_value=cm):
            result = get_video_info("https://youtu.be/abc")

        assert result["title"] == "Test video"
        assert result["duration"] == 120
        assert result["filesize"] == 5 * 1024 * 1024

    def test_fallback_title_when_missing(self):
        cm = self._mock_ydl_cm({"title": None, "duration": None})
        with patch("yt_dlp.YoutubeDL", return_value=cm):
            result = get_video_info("https://youtu.be/abc")
        assert result["title"] == "Sin título"

    def test_propagates_download_error(self):
        ydl = MagicMock()
        ydl.extract_info.side_effect = yt_dlp.DownloadError("private")
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl)
        cm.__exit__ = MagicMock(return_value=False)
        with patch("yt_dlp.YoutubeDL", return_value=cm):
            with pytest.raises(yt_dlp.DownloadError):
                get_video_info("https://youtu.be/abc")


class TestGetVideoDimensions:
    def _ffprobe_output(self, width: int, height: int) -> str:
        return json.dumps({"streams": [{"width": width, "height": height, "codec_type": "video"}]})

    def test_returns_width_and_height(self, tmp_path):
        fake = tmp_path / "video.mp4"
        fake.write_bytes(b"data")
        mock_result = MagicMock()
        mock_result.stdout = self._ffprobe_output(1080, 1920)

        with patch("downloader.subprocess.run", return_value=mock_result):
            w, h = get_video_dimensions(str(fake))

        assert w == 1080
        assert h == 1920

    def test_returns_zeros_on_empty_streams(self, tmp_path):
        fake = tmp_path / "video.mp4"
        fake.write_bytes(b"data")
        mock_result = MagicMock()
        mock_result.stdout = json.dumps({"streams": []})

        with patch("downloader.subprocess.run", return_value=mock_result):
            w, h = get_video_dimensions(str(fake))

        assert w == 0
        assert h == 0

    def test_returns_zeros_on_exception(self, tmp_path):
        with patch("downloader.subprocess.run", side_effect=Exception("ffprobe not found")):
            w, h = get_video_dimensions("nonexistent.mp4")

        assert w == 0
        assert h == 0


class TestDownloadAudio:
    def _mock_ydl(self, filename: str, info: dict) -> MagicMock:
        ydl = MagicMock()
        ydl.extract_info.return_value = info
        ydl.prepare_filename.return_value = filename
        return ydl

    def _make_cm(self, ydl: MagicMock) -> MagicMock:
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl)
        cm.__exit__ = MagicMock(return_value=False)
        return cm

    def test_returns_filepath_and_metadata(self, tmp_path):
        fake_mp3 = tmp_path / "audio.mp3"
        fake_mp3.write_bytes(b"data")
        info = {"title": "My Song", "track": "My Song", "artist": "Cool Artist"}
        ydl_mock = self._mock_ydl(str(tmp_path / "audio.webm"), info)
        cm = self._make_cm(ydl_mock)

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", return_value=cm):
            filepath, meta = download_audio("https://youtu.be/abc")

        assert filepath.endswith(".mp3")
        assert meta["title"] == "My Song"
        assert meta["artist"] == "Cool Artist"

    def test_uses_track_field_over_title(self, tmp_path):
        fake_mp3 = tmp_path / "audio.mp3"
        fake_mp3.write_bytes(b"data")
        info = {"title": "YouTube Title", "track": "Album Track Name", "artist": "Artist"}
        ydl_mock = self._mock_ydl(str(tmp_path / "audio.webm"), info)
        cm = self._make_cm(ydl_mock)

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", return_value=cm):
            _, meta = download_audio("https://youtu.be/abc")

        assert meta["title"] == "Album Track Name"

    def test_falls_back_to_title_when_no_track(self, tmp_path):
        fake_mp3 = tmp_path / "audio.mp3"
        fake_mp3.write_bytes(b"data")
        info = {"title": "Video Title", "track": None, "artist": None}
        ydl_mock = self._mock_ydl(str(tmp_path / "audio.webm"), info)
        cm = self._make_cm(ydl_mock)

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", return_value=cm):
            _, meta = download_audio("https://youtu.be/abc")

        assert meta["title"] == "Video Title"
        assert meta["artist"] is None

    def test_artist_falls_back_to_creator(self, tmp_path):
        fake_mp3 = tmp_path / "audio.mp3"
        fake_mp3.write_bytes(b"data")
        info = {"title": "Song", "track": None, "artist": None, "creator": "Channel Name"}
        ydl_mock = self._mock_ydl(str(tmp_path / "audio.webm"), info)
        cm = self._make_cm(ydl_mock)

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", return_value=cm):
            _, meta = download_audio("https://youtu.be/abc")

        assert meta["artist"] == "Channel Name"

    def test_propagates_download_error(self, tmp_path):
        ydl_mock = MagicMock()
        ydl_mock.extract_info.side_effect = yt_dlp.DownloadError("ERROR: 403")
        cm = self._make_cm(ydl_mock)

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", return_value=cm):
            with pytest.raises(yt_dlp.DownloadError):
                download_audio("https://youtu.be/bad")
