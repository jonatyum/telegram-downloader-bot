import pytest

from links import extract_urls, is_supported_url, is_youtube_url


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
        assert is_supported_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://reddit.com/r/videos/",
        "https://example.com/video.mp4",
        "not a url at all",
        "",
        "ftp://tiktok.com/video",
    ])
    def test_unsupported_urls(self, url):
        assert is_supported_url(url) is False

    def test_no_subdomain_spoofing(self):
        # "notiktok.com" should not match "tiktok.com"
        assert is_supported_url("https://notiktok.com/video") is False
        assert is_supported_url("https://faketiktok.com/video") is False


class TestIsYoutubeUrl:
    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc123",
        "https://youtube.com/shorts/abc",
        "https://music.youtube.com/watch?v=abc",
    ])
    def test_youtube_urls_detected(self, url):
        assert is_youtube_url(url) is True

    @pytest.mark.parametrize("url", [
        "https://www.tiktok.com/@user/video/123",
        "https://www.instagram.com/reel/abc/",
        "https://x.com/user/status/123",
    ])
    def test_non_youtube_urls_rejected(self, url):
        assert is_youtube_url(url) is False


class TestExtractUrls:
    def test_extracts_multiple_unique(self):
        text = "https://www.tiktok.com/@u/video/1 mira esto https://x.com/u/status/2"
        assert extract_urls(text) == [
            "https://www.tiktok.com/@u/video/1",
            "https://x.com/u/status/2",
        ]

    def test_dedupes_and_drops_unsupported(self):
        text = "https://youtu.be/a https://youtu.be/a https://reddit.com/x"
        assert extract_urls(text) == ["https://youtu.be/a"]

    def test_empty_when_none(self):
        assert extract_urls("solo texto sin links") == []
