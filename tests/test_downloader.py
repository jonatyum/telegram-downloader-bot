import json
import os
import pytest
import yt_dlp
import downloader
from unittest.mock import MagicMock, patch, call

from downloader import download_video, download_audio, download_post, _make_output_path, get_video_dimensions, get_video_info, get_audio_info, _estimate_filesize, _run_with_retry, _is_transient_error, fetch_thumbnail, _has_audio_stream, _warn_if_silent, _is_image_entry
from config import DOWNLOAD_DIR, MAX_DOWNLOAD_ATTEMPTS, MAX_VIDEO_HEIGHT


def _cm(ydl_mock):
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=ydl_mock)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestDownloadPost:
    """
    download_post extrae el post con download=False y luego baja cada item por
    separado: los videos con process_ie_result y las fotos desde su thumbnail
    (Instagram no las expone como formato descargable).
    """

    @staticmethod
    def _video_entry(path):
        # Un entry con formats/url es video: _is_image_entry lo descarta como foto.
        return {"id": "vid", "url": "https://cdn/v.mp4",
                "requested_downloads": [{"filepath": str(path)}]}

    @staticmethod
    def _photo_entry():
        # Solo thumbnails, sin formats ni url → _is_image_entry lo clasifica como foto.
        return {"id": "img", "thumbnails": [{"url": "https://cdn/p.jpg", "width": 1080, "height": 1080}]}

    def _run(self, entries, process_result=None, image_path=None, tmp_path=None):
        ydl = MagicMock()
        ydl.extract_info.return_value = {"entries": entries}
        ydl.process_ie_result.side_effect = (
            process_result if callable(process_result) else lambda e, download: e
        )

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("downloader._ensure_h264", side_effect=lambda p: p), \
             patch("downloader._download_image", return_value=image_path), \
             patch("yt_dlp.YoutubeDL", return_value=_cm(ydl)):
            return download_post("https://www.instagram.com/p/abc/"), ydl

    def test_returns_items_with_kinds(self, tmp_path):
        vid = tmp_path / "a.1.mp4"; vid.write_bytes(b"v")
        img = tmp_path / "a.2.jpg"; img.write_bytes(b"i")

        items, _ = self._run(
            entries=[self._video_entry(vid), self._photo_entry()],
            image_path=str(img),
            tmp_path=tmp_path,
        )

        assert [it["kind"] for it in items] == ["video", "photo"]
        assert items[0]["path"] == str(vid)
        assert items[1]["path"] == str(img)

    def test_photo_downloaded_from_best_thumbnail(self, tmp_path):
        img = tmp_path / "photo.jpg"; img.write_bytes(b"i")
        entry = {"id": "img", "thumbnails": [
            {"url": "https://cdn/small.jpg", "width": 320, "height": 320},
            {"url": "https://cdn/full.jpg", "width": 1080, "height": 1080},
        ]}

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("downloader._download_image", return_value=str(img)) as mock_dl, \
             patch("yt_dlp.YoutubeDL", return_value=_cm(self._ydl_with([entry]))):
            items = download_post("https://www.instagram.com/p/abc/")

        # Elige el thumbnail de mayor resolución = la foto full-size.
        mock_dl.assert_called_once_with("https://cdn/full.jpg")
        assert items == [{"path": str(img), "kind": "photo"}]

    def test_skips_photo_whose_download_fails(self, tmp_path):
        items, _ = self._run(
            entries=[self._photo_entry()], image_path=None, tmp_path=tmp_path
        )
        assert items == []

    def test_skips_items_missing_on_disk(self, tmp_path):
        items, _ = self._run(
            entries=[self._video_entry(tmp_path / "missing.mp4")], tmp_path=tmp_path
        )
        assert items == []

    def test_skips_video_item_that_fails_to_download(self, tmp_path):
        ok = tmp_path / "ok.mp4"; ok.write_bytes(b"v")

        def _process(entry, download):
            if entry["id"] == "boom":
                raise yt_dlp.DownloadError("item roto")
            return entry

        # Un item que revienta no debe tumbar el post entero.
        items, _ = self._run(
            entries=[{"id": "boom", "url": "https://cdn/x.mp4"}, self._video_entry(ok)],
            process_result=_process,
            tmp_path=tmp_path,
        )
        assert [it["path"] for it in items] == [str(ok)]

    def test_video_items_pass_through_ensure_h264(self, tmp_path):
        vid = tmp_path / "a.1.mp4"; vid.write_bytes(b"v")
        converted = tmp_path / "a.1_h264.mp4"; converted.write_bytes(b"v")

        ydl = self._ydl_with([self._video_entry(vid)])
        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("downloader._ensure_h264", return_value=str(converted)) as mock_h264, \
             patch("yt_dlp.YoutubeDL", return_value=_cm(ydl)):
            items = download_post("https://www.instagram.com/p/abc/")

        mock_h264.assert_called_once_with(str(vid))
        assert items[0]["path"] == str(converted)

    @staticmethod
    def _ydl_with(entries):
        ydl = MagicMock()
        ydl.extract_info.return_value = {"entries": entries}
        ydl.process_ie_result.side_effect = lambda e, download: e
        return ydl


class TestGetVideoInfoPlaylist:
    def test_detects_playlist_and_sums_size(self):
        ydl = MagicMock()
        ydl.extract_info.return_value = {
            "title": "Post",
            "entries": [{"filesize": 10}, {"filesize": 20}, None],
        }
        with patch("yt_dlp.YoutubeDL", return_value=_cm(ydl)):
            info = get_video_info("https://www.instagram.com/p/abc/")

        assert info["is_playlist"] is True
        assert info["count"] == 2
        assert info["filesize"] == 30

    def test_single_video_not_playlist(self):
        ydl = MagicMock()
        ydl.extract_info.return_value = {"title": "V", "duration": 10, "filesize": 5}
        with patch("yt_dlp.YoutubeDL", return_value=_cm(ydl)):
            info = get_video_info("https://www.tiktok.com/@u/video/1")

        assert info["is_playlist"] is False
        assert info["count"] == 1

    def test_playlist_includes_thumbnail_when_available(self):
        ydl = MagicMock()
        ydl.extract_info.return_value = {
            "title": "Post",
            "entries": [{"filesize": 10}, {"filesize": 20}],
            "thumbnails": [{"url": "https://cdn/post.jpg", "width": 640, "height": 640}],
        }
        with patch("yt_dlp.YoutubeDL", return_value=_cm(ydl)):
            info = get_video_info("https://www.instagram.com/p/abc/")

        assert info["thumbnail"] == "https://cdn/post.jpg"

    def test_playlist_thumbnail_none_when_missing(self):
        ydl = MagicMock()
        ydl.extract_info.return_value = {
            "title": "Post",
            "entries": [{"filesize": 10}, {"filesize": 20}],
        }
        with patch("yt_dlp.YoutubeDL", return_value=_cm(ydl)):
            info = get_video_info("https://www.instagram.com/p/abc/")

        assert info["thumbnail"] is None


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
             patch("downloader.time.sleep"), \
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
        assert str(MAX_VIDEO_HEIGHT) in captured_opts["format"]

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

    def test_includes_thumbnail_when_available(self):
        cm = self._mock_ydl_cm({
            "title": "Test video", "duration": 120, "filesize": 5,
            "thumbnails": [
                {"url": "https://cdn/small.jpg", "width": 120, "height": 120},
                {"url": "https://cdn/big.jpg", "width": 1280, "height": 720},
            ],
        })
        with patch("yt_dlp.YoutubeDL", return_value=cm):
            result = get_video_info("https://youtu.be/abc")
        # Elige el thumbnail de mayor resolución, igual que download_post.
        assert result["thumbnail"] == "https://cdn/big.jpg"

    def test_thumbnail_none_when_missing(self):
        cm = self._mock_ydl_cm({"title": "V", "duration": 10})
        with patch("yt_dlp.YoutubeDL", return_value=cm):
            result = get_video_info("https://youtu.be/abc")
        assert result["thumbnail"] is None

    def test_propagates_download_error(self):
        ydl = MagicMock()
        ydl.extract_info.side_effect = yt_dlp.DownloadError("private")
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl)
        cm.__exit__ = MagicMock(return_value=False)
        with patch("yt_dlp.YoutubeDL", return_value=cm):
            with pytest.raises(yt_dlp.DownloadError):
                get_video_info("https://youtu.be/abc")


class TestFetchThumbnail:
    def _mock_response(self, data: bytes):
        resp = MagicMock()
        resp.read.return_value = data
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_returns_bytes_on_success(self):
        with patch("downloader.urllib.request.urlopen", return_value=self._mock_response(b"jpegdata")):
            result = fetch_thumbnail("https://cdn/thumb.jpg")
        assert result == b"jpegdata"

    def test_none_on_network_error(self):
        with patch("downloader.urllib.request.urlopen", side_effect=OSError("boom")):
            assert fetch_thumbnail("https://cdn/thumb.jpg") is None

    def test_none_when_exceeds_max_bytes(self):
        with patch("downloader.urllib.request.urlopen", return_value=self._mock_response(b"x" * 100)):
            assert fetch_thumbnail("https://cdn/thumb.jpg", max_bytes=50) is None

    def test_accepts_exactly_max_bytes(self):
        with patch("downloader.urllib.request.urlopen", return_value=self._mock_response(b"x" * 50)):
            assert fetch_thumbnail("https://cdn/thumb.jpg", max_bytes=50) == b"x" * 50


class TestCookiefile:
    """
    _cookiefile() copia el cookies.txt a un sitio escribible: yt-dlp reescribe el
    cookiefile al cerrar y en Render el secreto se monta de solo lectura.
    """

    @staticmethod
    def _reset():
        downloader._cookies_prepared = False
        downloader._cookies_workfile = None

    def test_none_when_not_configured(self):
        self._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""):
            assert downloader._cookiefile() is None

    def test_none_when_path_missing(self, tmp_path):
        self._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", str(tmp_path / "no-existe.txt")):
            assert downloader._cookiefile() is None

    def test_copies_to_writable_location(self, tmp_path):
        self._reset()
        src = tmp_path / "secret" / "cookies.txt"
        src.parent.mkdir()
        src.write_text("# Netscape HTTP Cookie File\n")
        work = tmp_path / "downloads"
        with patch("downloader.YOUTUBE_COOKIES_FILE", str(src)), \
             patch("downloader.DOWNLOAD_DIR", str(work)):
            got = downloader._cookiefile()
        assert got is not None
        assert got != str(src)  # nunca el original: es de solo lectura en Render
        assert os.path.exists(got)
        assert open(got).read() == "# Netscape HTTP Cookie File\n"

    def test_copy_is_made_only_once(self, tmp_path):
        self._reset()
        src = tmp_path / "cookies.txt"; src.write_text("a")
        work = tmp_path / "dl"
        with patch("downloader.YOUTUBE_COOKIES_FILE", str(src)), \
             patch("downloader.DOWNLOAD_DIR", str(work)), \
             patch("downloader.shutil.copyfile") as copyfile:
            downloader._cookiefile()
            downloader._cookiefile()
            downloader._cookiefile()
        assert copyfile.call_count == 1

    def test_base_opts_omits_cookiefile_without_cookies(self):
        self._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""):
            assert "cookiefile" not in downloader._base_opts()

    def test_base_opts_includes_cookiefile_when_configured(self, tmp_path):
        self._reset()
        src = tmp_path / "cookies.txt"; src.write_text("a")
        work = tmp_path / "dl"
        with patch("downloader.YOUTUBE_COOKIES_FILE", str(src)), \
             patch("downloader.DOWNLOAD_DIR", str(work)):
            opts = downloader._base_opts()
        assert opts["cookiefile"] == downloader._cookies_workfile


class TestYoutubePlayerClients:
    """
    Los clientes de InnerTube van en _base_opts para que el preflight extraiga con los
    mismos que la descarga real: si difirieran, el preflight aprobaría links que después
    no se pueden bajar.
    """

    def test_base_opts_carries_configured_clients(self):
        TestCookiefile._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""), \
             patch("downloader.YOUTUBE_PLAYER_CLIENTS", ("default", "tv_simply")):
            opts = downloader._base_opts()
        assert opts["extractor_args"]["youtube"]["player_client"] == ["default", "tv_simply"]

    def test_base_opts_omits_extractor_args_when_empty(self):
        TestCookiefile._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""), \
             patch("downloader.YOUTUBE_PLAYER_CLIENTS", ()):
            assert "extractor_args" not in downloader._base_opts()

    def test_download_opts_merges_instead_of_replacing(self):
        """La clave del bug: una llamada con su propio extractor_args no puede borrar los clientes."""
        TestCookiefile._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""), \
             patch("downloader.YOUTUBE_PLAYER_CLIENTS", ("default", "android_vr")):
            opts = downloader._download_opts(
                "/tmp/x.%(ext)s", None,
                extractor_args={"tiktok": {"webpage_download": True}},
            )
        assert opts["extractor_args"]["youtube"]["player_client"] == ["default", "android_vr"]
        assert opts["extractor_args"]["tiktok"] == {"webpage_download": True}

    def test_download_opts_merges_within_the_same_extractor(self):
        """download_audio pasa youtube:skip; tiene que convivir con youtube:player_client."""
        TestCookiefile._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""), \
             patch("downloader.YOUTUBE_PLAYER_CLIENTS", ("tv_simply",)):
            opts = downloader._download_opts(
                "/tmp/x.%(ext)s", None,
                extractor_args={"youtube": {"skip": ["dash", "hls"]}},
            )
        assert opts["extractor_args"]["youtube"] == {
            "player_client": ["tv_simply"],
            "skip": ["dash", "hls"],
        }

    def test_base_opts_is_not_mutated_by_a_download_opts_call(self):
        """El merge copia: si mutara el dict de la base, la segunda llamada arrastraría lo de la primera."""
        TestCookiefile._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""), \
             patch("downloader.YOUTUBE_PLAYER_CLIENTS", ("default",)):
            downloader._download_opts(
                "/tmp/x.%(ext)s", None,
                extractor_args={"youtube": {"skip": ["dash"]}},
            )
            segunda = downloader._download_opts("/tmp/y.%(ext)s", None)
        assert segunda["extractor_args"]["youtube"] == {"player_client": ["default"]}


class TestYoutubeProxy:
    """
    El proxy es la única salida al bloqueo por reputación de IP que no depende de
    cookies, pero se paga por GB: solo se aplica a YouTube.
    """

    def test_not_applied_when_unset(self):
        TestCookiefile._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""), \
             patch("downloader.YOUTUBE_PROXY", ""):
            assert "proxy" not in downloader._base_opts(youtube=True)

    def test_applied_to_youtube(self):
        TestCookiefile._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""), \
             patch("downloader.YOUTUBE_PROXY", "http://user:pass@proxy:8080"):
            assert downloader._base_opts(youtube=True)["proxy"] == "http://user:pass@proxy:8080"

    def test_not_applied_to_other_platforms(self):
        """Un TikTok no debe salir por un proxy que se paga por GB: baja directo."""
        TestCookiefile._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""), \
             patch("downloader.YOUTUBE_PROXY", "http://user:pass@proxy:8080"):
            assert "proxy" not in downloader._base_opts(youtube=False)
            assert "proxy" not in downloader._download_opts("/tmp/x.%(ext)s", None)

    def test_wired_through_the_public_functions(self):
        """Lo que importa no es la opción sino el cableado: cada función decide por su URL."""
        TestCookiefile._reset()
        ydl = MagicMock()
        ydl.extract_info.return_value = {"title": "t", "duration": 1}
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=ydl)
        cm.__exit__ = MagicMock(return_value=False)

        with patch("downloader.YOUTUBE_COOKIES_FILE", ""), \
             patch("downloader.YOUTUBE_PROXY", "http://proxy:8080"), \
             patch("yt_dlp.YoutubeDL", return_value=cm) as ydl_cls:
            get_video_info("https://youtu.be/abc")
            assert ydl_cls.call_args[0][0]["proxy"] == "http://proxy:8080"

            ydl_cls.reset_mock()
            get_video_info("https://www.tiktok.com/@u/video/1")
            assert "proxy" not in ydl_cls.call_args[0][0]

    def test_download_opts_forwards_the_flag(self):
        TestCookiefile._reset()
        with patch("downloader.YOUTUBE_COOKIES_FILE", ""), \
             patch("downloader.YOUTUBE_PROXY", "socks5://proxy:1080"):
            opts = downloader._download_opts("/tmp/x.%(ext)s", None, youtube=True)
        assert opts["proxy"] == "socks5://proxy:1080"


class TestIsImageEntry:
    def test_instagram_photo_without_formats_is_image(self):
        entry = {"extractor_key": "Instagram", "thumbnails": [{"url": "https://cdn/p.jpg"}]}
        assert _is_image_entry(entry) is True

    def test_entry_with_formats_is_not_image(self):
        entry = {"extractor_key": "Instagram", "formats": [{"url": "https://cdn/v.mp4"}],
                  "thumbnails": [{"url": "https://cdn/p.jpg"}]}
        assert _is_image_entry(entry) is False

    def test_youtube_without_formats_is_never_image(self):
        # YouTube no publica fotos: sin formats es un fallo de extracción (throttling,
        # bot-detection...), nunca un post de imagen real — bajarlo como foto entregaría
        # el thumbnail en vez del video.
        entry = {"extractor_key": "Youtube", "thumbnails": [{"url": "https://i.ytimg.com/x.jpg"}]}
        assert _is_image_entry(entry) is False

    def test_youtube_extractor_lowercase_field_also_excluded(self):
        entry = {"extractor": "youtube", "thumbnails": [{"url": "https://i.ytimg.com/x.jpg"}]}
        assert _is_image_entry(entry) is False


class TestHasAudioStream:
    def test_true_when_ffprobe_lists_an_audio_stream(self, tmp_path):
        fake = tmp_path / "v.mp4"; fake.write_bytes(b"d")
        mock_result = MagicMock(stdout="0\n")  # ffprobe lista el índice del stream de audio
        with patch("downloader.subprocess.run", return_value=mock_result):
            assert _has_audio_stream(str(fake)) is True

    def test_false_when_ffprobe_lists_nothing(self, tmp_path):
        fake = tmp_path / "v.mp4"; fake.write_bytes(b"d")
        mock_result = MagicMock(stdout="")  # sin streams de audio
        with patch("downloader.subprocess.run", return_value=mock_result):
            assert _has_audio_stream(str(fake)) is False

    def test_true_on_ffprobe_failure(self, tmp_path):
        # Si ffprobe falla, no queremos bloquear la entrega por un chequeo que no corrió.
        fake = tmp_path / "v.mp4"; fake.write_bytes(b"d")
        with patch("downloader.subprocess.run", side_effect=Exception("ffprobe no encontrado")):
            assert _has_audio_stream(str(fake)) is True


class TestWarnIfSilent:
    def test_logs_warning_when_no_audio(self, tmp_path, caplog):
        fake = tmp_path / "v.mp4"; fake.write_bytes(b"d")
        with patch("downloader._has_audio_stream", return_value=False):
            with caplog.at_level("WARNING", logger="downloader"):
                _warn_if_silent(str(fake), "https://tiktok.com/@u/video/1", 2160)
        assert any("sin pista de audio" in r.message for r in caplog.records)

    def test_no_warning_when_audio_present(self, tmp_path, caplog):
        fake = tmp_path / "v.mp4"; fake.write_bytes(b"d")
        with patch("downloader._has_audio_stream", return_value=True):
            with caplog.at_level("WARNING", logger="downloader"):
                _warn_if_silent(str(fake), "https://tiktok.com/@u/video/1", 2160)
        assert not any("sin pista de audio" in r.message for r in caplog.records)


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
             patch("downloader.time.sleep"), \
             patch("yt_dlp.YoutubeDL", return_value=cm):
            with pytest.raises(yt_dlp.DownloadError):
                download_audio("https://youtu.be/bad")


class TestRetry:
    def test_is_transient_for_generic_error(self):
        assert _is_transient_error(yt_dlp.DownloadError("ERROR: HTTP Error 403: Forbidden")) is True
        assert _is_transient_error(yt_dlp.DownloadError("Unable to extract video data")) is True

    def test_is_not_transient_for_permanent_errors(self):
        assert _is_transient_error(yt_dlp.DownloadError("ERROR: This video is private")) is False
        assert _is_transient_error(yt_dlp.DownloadError("Video unavailable")) is False
        assert _is_transient_error(yt_dlp.DownloadError("File is larger than max-filesize")) is False
        assert _is_transient_error(yt_dlp.DownloadError("Requested format is not available")) is False

    def test_youtube_bot_check_is_permanent(self):
        """Reintentar el chequeo antibot no cambia nada: la IP del host sigue siendo la misma."""
        # YouTube manda el apóstrofo tipográfico, así que se prueban las dos formas.
        assert _is_transient_error(yt_dlp.DownloadError(
            "ERROR: [youtube] abc: Sign in to confirm you\u2019re not a bot. Use --cookies")) is False
        assert _is_transient_error(yt_dlp.DownloadError(
            "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies")) is False

    def test_retries_transient_then_succeeds(self):
        calls = []

        def op():
            calls.append(1)
            if len(calls) < 2:
                raise yt_dlp.DownloadError("ERROR: 503 Service Unavailable temporarily")
            return "ok"

        with patch("downloader.time.sleep") as sleep_mock:
            assert _run_with_retry(op) == "ok"

        assert len(calls) == 2
        sleep_mock.assert_called_once()

    def test_does_not_retry_permanent_error(self):
        calls = []

        def op():
            calls.append(1)
            raise yt_dlp.DownloadError("ERROR: This video is private")

        with patch("downloader.time.sleep"):
            with pytest.raises(yt_dlp.DownloadError):
                _run_with_retry(op)

        assert len(calls) == 1

    def test_gives_up_after_max_attempts(self):
        calls = []

        def op():
            calls.append(1)
            raise yt_dlp.DownloadError("ERROR: temporary network glitch")

        with patch("downloader.time.sleep"):
            with pytest.raises(yt_dlp.DownloadError):
                _run_with_retry(op)

        assert len(calls) == MAX_DOWNLOAD_ATTEMPTS

    def test_no_retry_on_success(self):
        calls = []

        def op():
            calls.append(1)
            return "done"

        with patch("downloader.time.sleep") as sleep_mock:
            assert _run_with_retry(op) == "done"

        assert len(calls) == 1
        sleep_mock.assert_not_called()


class TestCompressVideo:
    def _src(self, tmp_path):
        src = tmp_path / "v.mp4"
        src.write_bytes(b"x" * 100)
        return str(src)

    def test_none_when_duration_unknown(self, tmp_path):
        from downloader import compress_video
        with patch("downloader._probe_duration", return_value=None):
            assert compress_video(self._src(tmp_path), 50 * 1024 * 1024) is None

    def test_none_when_bitrate_too_low(self, tmp_path):
        from downloader import compress_video
        # duración enorme → bitrate objetivo ínfimo → no vale la pena
        with patch("downloader._probe_duration", return_value=100_000):
            assert compress_video(self._src(tmp_path), 50 * 1024 * 1024) is None

    def test_success_returns_compressed_path(self, tmp_path):
        from downloader import compress_video
        src = self._src(tmp_path)
        out = src.rsplit(".", 1)[0] + "_compressed.mp4"

        def fake_run(cmd, **kw):
            with open(out, "wb") as f:
                f.write(b"y" * 10)
            m = MagicMock(); m.returncode = 0; m.stderr = b""
            return m

        with patch("downloader._probe_duration", return_value=60), \
             patch("downloader.subprocess.run", side_effect=fake_run):
            res = compress_video(src, 50 * 1024 * 1024)

        assert res == out
        assert os.path.exists(out)

    def test_none_and_cleanup_when_still_too_big(self, tmp_path):
        from downloader import compress_video
        src = self._src(tmp_path)
        out = src.rsplit(".", 1)[0] + "_compressed.mp4"

        def fake_run(cmd, **kw):
            with open(out, "wb") as f:
                f.write(b"y")
            m = MagicMock(); m.returncode = 0; m.stderr = b""
            return m

        with patch("downloader._probe_duration", return_value=60), \
             patch("downloader.subprocess.run", side_effect=fake_run), \
             patch("downloader.os.path.getsize", return_value=50 * 1024 * 1024 + 1):
            res = compress_video(src, 50 * 1024 * 1024)

        assert res is None
        assert not os.path.exists(out)

    def test_none_when_ffmpeg_fails(self, tmp_path):
        from downloader import compress_video
        m = MagicMock(); m.returncode = 1; m.stderr = b"boom"
        with patch("downloader._probe_duration", return_value=60), \
             patch("downloader.subprocess.run", return_value=m):
            assert compress_video(self._src(tmp_path), 50 * 1024 * 1024) is None


class TestIdentifySong:
    def test_real_song(self):
        from downloader import _identify_song
        assert _identify_song({"track": "Flowers", "artist": "Miley Cyrus"}) == {
            "track": "Flowers", "artist": "Miley Cyrus"
        }

    def test_original_sound_returns_none(self):
        from downloader import _identify_song
        assert _identify_song({"track": "original sound - bob", "artist": "bob"}) is None
        assert _identify_song({"track": "sonido original - ana", "artist": "ana"}) is None

    def test_missing_track_or_artist_returns_none(self):
        from downloader import _identify_song
        assert _identify_song({"track": "Flowers"}) is None
        assert _identify_song({"artist": "Miley Cyrus"}) is None

    def test_falls_back_to_artists_list(self):
        from downloader import _identify_song
        assert _identify_song({"track": "X", "artists": ["A"]})["artist"] == "A"


class TestDownloadSong:
    def test_searches_youtube_and_returns_mp3(self, tmp_path):
        from downloader import download_song
        entry = {"title": "Flowers", "artist": "Miley Cyrus", "ext": "webm"}
        ydl = MagicMock()
        ydl.extract_info.return_value = {"entries": [entry]}
        ydl.prepare_filename.return_value = str(tmp_path / "x.webm")

        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("yt_dlp.YoutubeDL", return_value=_cm(ydl)):
            path, meta = download_song("Miley Cyrus Flowers")

        assert path.endswith(".mp3")
        assert meta == {"title": "Flowers", "artist": "Miley Cyrus"}
        assert ydl.extract_info.call_args[0][0].startswith("ytsearch1:")

    def test_raises_when_search_fails(self, tmp_path):
        from downloader import download_song
        ydl = MagicMock()
        ydl.extract_info.side_effect = yt_dlp.DownloadError("No video results")
        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("downloader.time.sleep"), \
             patch("yt_dlp.YoutubeDL", return_value=_cm(ydl)):
            with pytest.raises(yt_dlp.DownloadError):
                download_song("nonexistent song xyz")

    def test_raises_when_entry_empty(self, tmp_path):
        from downloader import download_song
        ydl = MagicMock()
        ydl.extract_info.return_value = {"entries": [None]}
        with patch("downloader.DOWNLOAD_DIR", str(tmp_path)), \
             patch("downloader.time.sleep"), \
             patch("yt_dlp.YoutubeDL", return_value=_cm(ydl)):
            with pytest.raises(yt_dlp.DownloadError):
                download_song("x")
