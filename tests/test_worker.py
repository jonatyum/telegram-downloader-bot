import json
import os

import httpx
import pytest
import yt_dlp
from httpx import ASGITransport
from unittest.mock import patch

import worker

TOKEN = "un-token-de-prueba"


def _client() -> httpx.AsyncClient:
    # Igual que test_api.py: cliente async sobre la app, no el TestClient síncrono.
    return httpx.AsyncClient(transport=ASGITransport(app=worker.app), base_url="http://worker")


@pytest.fixture(autouse=True)
def _token():
    with patch("worker.YOUTUBE_WORKER_TOKEN", TOKEN):
        yield


class TestAuth:
    """El túnel deja esto abierto a internet: sin token no se toca la conexión de casa."""

    async def test_health_needs_no_token(self):
        async with _client() as c:
            resp = await c.get("/health")
        assert resp.status_code == 200

    async def test_rejects_missing_token(self):
        async with _client() as c:
            resp = await c.post("/info", json={"url": "https://youtu.be/abc"})
        assert resp.status_code == 401

    async def test_rejects_wrong_token(self):
        async with _client() as c:
            resp = await c.post("/info", json={"url": "https://youtu.be/abc"},
                                headers={"X-Worker-Token": "otro"})
        assert resp.status_code == 401

    async def test_refuses_to_work_without_a_configured_token(self):
        """Un worker sin token sería una descarga gratis para cualquiera: mejor no arrancar."""
        with patch("worker.YOUTUBE_WORKER_TOKEN", ""):
            async with _client() as c:
                resp = await c.post("/info", json={"url": "https://youtu.be/abc"},
                                    headers={"X-Worker-Token": "lo-que-sea"})
        assert resp.status_code == 503


class TestInfo:
    async def test_returns_metadata(self):
        info = {"title": "Un video", "duration": 30, "filesize": 1000}
        with patch("worker.get_video_info", return_value=info) as spy:
            async with _client() as c:
                resp = await c.post("/info", json={"url": "https://youtu.be/abc", "max_height": 720},
                                    headers={"X-Worker-Token": TOKEN})
        assert resp.status_code == 200
        assert resp.json() == info
        assert spy.call_args[0] == ("https://youtu.be/abc", 720)

    async def test_download_error_becomes_422(self):
        """
        422 es el contrato con el cliente: "el worker anduvo, yt-dlp falló". El servidor
        propaga ese mensaje en vez de reintentar en local, que daría el chequeo antibot
        en lugar del motivo real.
        """
        with patch("worker.get_video_info", side_effect=yt_dlp.DownloadError("Video privado")):
            async with _client() as c:
                resp = await c.post("/info", json={"url": "https://youtu.be/abc"},
                                    headers={"X-Worker-Token": TOKEN})
        assert resp.status_code == 422
        assert "privado" in resp.json()["detail"]

    async def test_audio_info(self):
        with patch("worker.get_audio_info", return_value={"filesize": 4321}):
            async with _client() as c:
                resp = await c.post("/audio-info", json={"url": "https://youtu.be/abc"},
                                    headers={"X-Worker-Token": TOKEN})
        assert resp.json() == {"filesize": 4321}


class TestDownloads:
    async def test_video_is_returned_and_then_deleted(self, tmp_path):
        """Esta máquina es intermediaria, no almacén: el temporal se borra al enviarlo."""
        f = tmp_path / "video.mp4"
        f.write_bytes(b"contenido-de-video")

        with patch("worker.download_video", return_value=str(f)):
            async with _client() as c:
                resp = await c.post("/video", json={"url": "https://youtu.be/abc", "max_height": 720},
                                    headers={"X-Worker-Token": TOKEN})

        assert resp.status_code == 200
        assert resp.content == b"contenido-de-video"
        assert resp.headers["X-Filename"] == "video.mp4"
        assert not os.path.exists(f)

    async def test_audio_carries_metadata_in_a_header(self, tmp_path):
        f = tmp_path / "cancion.mp3"
        f.write_bytes(b"mp3")
        meta = {"title": "Título", "artist": "Alguien"}

        with patch("worker.download_audio", return_value=(str(f), meta)):
            async with _client() as c:
                resp = await c.post("/audio", json={"url": "https://youtu.be/abc"},
                                    headers={"X-Worker-Token": TOKEN})

        assert resp.status_code == 200
        assert json.loads(resp.headers["X-Meta"]) == meta

    async def test_metadata_header_survives_non_ascii(self, tmp_path):
        """Las cabeceras HTTP son latin-1: un título con acentos o emoji no puede romperlas."""
        f = tmp_path / "c.mp3"
        f.write_bytes(b"mp3")
        meta = {"title": "Canción 🚀", "artist": "Ñandú"}

        with patch("worker.download_audio", return_value=(str(f), meta)):
            async with _client() as c:
                resp = await c.post("/audio", json={"url": "https://youtu.be/abc"},
                                    headers={"X-Worker-Token": TOKEN})

        assert resp.status_code == 200
        assert json.loads(resp.headers["X-Meta"]) == meta

    async def test_song_takes_a_query(self, tmp_path):
        f = tmp_path / "s.mp3"
        f.write_bytes(b"mp3")

        with patch("worker.download_song", return_value=(str(f), {"title": "t", "artist": None})) as spy:
            async with _client() as c:
                resp = await c.post("/song", json={"query": "una canción"},
                                    headers={"X-Worker-Token": TOKEN})

        assert resp.status_code == 200
        assert spy.call_args[0][0] == "una canción"
