"""
Tests de api.py contra un ASGITransport en proceso (sin abrir un puerto real). Se usa
httpx.AsyncClient en vez del TestClient síncrono de FastAPI porque create_job agenda un
asyncio.create_task en segundo plano: con un cliente async en el mismo loop del test se
puede esperar a que ese task avance de verdad, en vez de que quede colgado a medias.

El motor se parchea igual que en test_pipeline.py: por nombre, en el módulo que lo llama
(aquí, api.py), dejando correr el asyncio real.
"""
import asyncio
import json
import os
from unittest.mock import patch

import pytest
import yt_dlp
from httpx import ASGITransport, AsyncClient

import api


@pytest.fixture(autouse=True)
def _clean_state(tmp_path):
    """
    Aísla cada test: jobs, resultados y el estado del rate limiter (un singleton de
    módulo) no deben filtrarse entre tests.
    """
    api._jobs.clear()
    api._limiter._timestamps.clear()
    with patch("api.RESULTS_DIR", str(tmp_path)):
        yield
    api._jobs.clear()
    api._limiter._timestamps.clear()


@pytest.fixture
async def client():
    transport = ASGITransport(app=api.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _wait_until(predicate, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError("condición no se cumplió a tiempo")
        await asyncio.sleep(0.01)


def _default_info(**overrides):
    return {"title": "Test", "filesize": None, "duration": 10, **overrides}


# ---------------------------------------------------------------------------
# POST /jobs — validación
# ---------------------------------------------------------------------------

class TestPreview:
    async def test_rejects_unsupported_url(self, client):
        r = await client.post("/preview", json={"url": "https://example.com/x"})
        assert r.status_code == 400

    async def test_returns_title_and_thumbnail(self, client):
        with patch("api.get_video_info", return_value=_default_info(title="Un video", duration=42, thumbnail="https://cdn/t.jpg")), \
             patch("api.fetch_thumbnail", return_value=b"\xff\xd8\xffjpegbytes"):
            r = await client.post("/preview", json={"url": "https://www.tiktok.com/@u/video/1"})

        assert r.status_code == 200
        body = r.json()
        assert body["title"] == "Un video"
        assert body["duration"] == 42
        assert body["thumbnail"].startswith("data:image/jpeg;base64,")

    async def test_no_thumbnail_field_when_platform_has_none(self, client):
        with patch("api.get_video_info", return_value=_default_info(thumbnail=None)):
            r = await client.post("/preview", json={"url": "https://www.tiktok.com/@u/video/1"})
        assert r.status_code == 200
        assert r.json()["thumbnail"] is None

    async def test_thumbnail_none_when_fetch_fails(self, client):
        with patch("api.get_video_info", return_value=_default_info(thumbnail="https://cdn/t.jpg")), \
             patch("api.fetch_thumbnail", return_value=None):
            r = await client.post("/preview", json={"url": "https://www.tiktok.com/@u/video/1"})
        assert r.status_code == 200
        assert r.json()["thumbnail"] is None

    async def test_does_not_start_a_job(self, client):
        with patch("api.get_video_info", return_value=_default_info(thumbnail=None)):
            await client.post("/preview", json={"url": "https://www.tiktok.com/@u/video/1"})
        assert api._jobs == {}

    async def test_download_error_maps_to_422(self, client):
        with patch("api.get_video_info", side_effect=yt_dlp.DownloadError("ERROR: This video is private")):
            r = await client.post("/preview", json={"url": "https://www.tiktok.com/@u/video/1"})
        assert r.status_code == 422
        assert "🔒" in r.json()["detail"]

    async def test_sniffs_png_thumbnail(self, client):
        png_magic = b"\x89PNG\r\n\x1a\n" + b"restofpng"
        with patch("api.get_video_info", return_value=_default_info(thumbnail="https://cdn/t.png")), \
             patch("api.fetch_thumbnail", return_value=png_magic):
            r = await client.post("/preview", json={"url": "https://www.tiktok.com/@u/video/1"})
        assert r.json()["thumbnail"].startswith("data:image/png;base64,")

    async def test_playlist_reports_count(self, client):
        with patch("api.get_video_info", return_value=_default_info(is_playlist=True, count=4, thumbnail=None)):
            r = await client.post("/preview", json={"url": "https://www.instagram.com/p/abc/"})
        body = r.json()
        assert body["is_playlist"] is True
        assert body["count"] == 4

    async def test_rate_limited_same_as_jobs(self, client):
        with patch("api.get_video_info", return_value=_default_info(thumbnail=None)):
            statuses = []
            for _ in range(api._RATE_LIMIT_MAX_REQUESTS + 2):
                r = await client.post(
                    "/preview", json={"url": "https://www.tiktok.com/@u/video/1"},
                    headers={"x-forwarded-for": "198.51.100.9"},
                )
                statuses.append(r.status_code)
        assert 429 in statuses


class TestCreateJobValidation:
    async def test_rejects_unsupported_url(self, client):
        r = await client.post("/jobs", json={"url": "https://example.com/x"})
        assert r.status_code == 400

    async def test_rejects_invalid_kind(self, client):
        r = await client.post("/jobs", json={"url": "https://www.tiktok.com/@u/video/1", "kind": "gif"})
        assert r.status_code == 422

    async def test_rejects_invalid_resolution(self, client):
        r = await client.post("/jobs", json={"url": "https://www.tiktok.com/@u/video/1", "resolution": 999})
        assert r.status_code == 422

    async def test_accepts_valid_resolution(self, client, tmp_path):
        fake = tmp_path / "src" / "video.mp4"; fake.parent.mkdir(); fake.write_bytes(b"d")
        with patch("api.get_video_info", return_value=_default_info()), \
             patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            r = await client.post("/jobs", json={"url": "https://www.tiktok.com/@u/video/1", "resolution": 720})
            job_id = r.json()["job_id"]
            await _wait_until(lambda: api._jobs[job_id].status in ("ready", "error"))
        assert r.status_code == 202


# ---------------------------------------------------------------------------
# Flujo completo: video pequeño listo para descargar
# ---------------------------------------------------------------------------

class TestVideoFlow:
    async def test_video_ready_and_downloadable(self, client, tmp_path):
        fake = tmp_path / "src" / "video.mp4"
        fake.parent.mkdir()
        fake.write_bytes(b"hola mundo")

        with patch("api.get_video_info", return_value=_default_info()), \
             patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(1280, 720)):
            r = await client.post("/jobs", json={"url": "https://www.tiktok.com/@u/video/1"})
            job_id = r.json()["job_id"]
            job = api._jobs[job_id]
            await _wait_until(lambda: job.status in ("ready", "error"))

        assert job.status == "ready"
        assert not fake.exists()  # se movió, no se copió

        status = await client.get(f"/jobs/{job_id}")
        assert status.json()["status"] == "ready"

        f = await client.get(f"/jobs/{job_id}/file")
        assert f.status_code == 200
        assert f.content == b"hola mundo"

    async def test_download_error_surfaces_as_job_error(self, client):
        with patch("api.get_video_info", return_value=_default_info()), \
             patch("pipeline.download_video", side_effect=yt_dlp.DownloadError("ERROR: This video is private")):
            r = await client.post("/jobs", json={"url": "https://www.tiktok.com/@u/video/1"})
            job_id = r.json()["job_id"]
            job = api._jobs[job_id]
            await _wait_until(lambda: job.status in ("ready", "error"))

        assert job.status == "error"
        assert "🔒" in job.error

        f = await client.get(f"/jobs/{job_id}/file")
        assert f.status_code == 404

    async def test_unexpected_exception_becomes_generic_error(self, client):
        with patch("api.get_video_info", return_value=_default_info()), \
             patch("pipeline.download_video", side_effect=RuntimeError("boom")):
            r = await client.post("/jobs", json={"url": "https://www.tiktok.com/@u/video/1"})
            job_id = r.json()["job_id"]
            job = api._jobs[job_id]
            await _wait_until(lambda: job.status in ("ready", "error"))

        assert job.status == "error"
        assert job.error == "Ocurrió un error inesperado. Inténtalo de nuevo."


# ---------------------------------------------------------------------------
# Carrusel: varios archivos por índice
# ---------------------------------------------------------------------------

class TestCarouselFlow:
    async def test_multiple_files_served_by_index(self, client, tmp_path):
        f1 = tmp_path / "src" / "1.mp4"; f1.parent.mkdir(); f1.write_bytes(b"v")
        f2 = tmp_path / "src" / "2.jpg"; f2.write_bytes(b"i")
        items = [{"path": str(f1), "kind": "video"}, {"path": str(f2), "kind": "photo"}]

        with patch("api.get_video_info", return_value=_default_info(is_playlist=True, count=2)), \
             patch("pipeline.download_post", return_value=items):
            r = await client.post("/jobs", json={"url": "https://www.instagram.com/p/abc/"})
            job_id = r.json()["job_id"]
            job = api._jobs[job_id]
            await _wait_until(lambda: job.status in ("ready", "error"))

        assert job.status == "ready"
        r0 = await client.get(f"/jobs/{job_id}/file", params={"index": 0})
        r1 = await client.get(f"/jobs/{job_id}/file", params={"index": 1})
        assert r0.content == b"v"
        assert r1.content == b"i"

        r2 = await client.get(f"/jobs/{job_id}/file", params={"index": 2})
        assert r2.status_code == 404


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------

class TestJobEvents:
    async def test_unknown_job_404(self, client):
        r = await client.get("/jobs/doesnotexist/events")
        assert r.status_code == 404

    async def test_replays_ready_state_immediately(self, client, tmp_path):
        fake = tmp_path / "src" / "video.mp4"; fake.parent.mkdir(); fake.write_bytes(b"d")

        with patch("api.get_video_info", return_value=_default_info()), \
             patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            r = await client.post("/jobs", json={"url": "https://www.tiktok.com/@u/video/1"})
            job_id = r.json()["job_id"]
            job = api._jobs[job_id]
            await _wait_until(lambda: job.status in ("ready", "error"))

        # Un cliente que se conecta DESPUÉS de que el job terminó debe recibir el
        # estado final al instante, no quedarse esperando un evento que ya pasó.
        async with client.stream("GET", f"/jobs/{job_id}/events") as resp:
            body = b""
            async for chunk in resp.aiter_bytes():
                body += chunk
                if b"\n\n" in body:
                    break
        events = [json.loads(l[6:]) for l in body.decode().strip().split("\n\n") if l.startswith("data: ")]
        assert any(e["type"] == "ready" for e in events) or events[0]["type"] == "status"


# ---------------------------------------------------------------------------
# Ciclo de vida del job
# ---------------------------------------------------------------------------

class TestJobLifecycle:
    async def test_not_found_returns_404(self, client):
        r = await client.get("/jobs/doesnotexist")
        assert r.status_code == 404
        r = await client.get("/jobs/doesnotexist/file")
        assert r.status_code == 404

    async def test_delete_removes_job_and_file(self, client, tmp_path):
        fake = tmp_path / "src" / "video.mp4"; fake.parent.mkdir(); fake.write_bytes(b"d")

        with patch("api.get_video_info", return_value=_default_info()), \
             patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            r = await client.post("/jobs", json={"url": "https://www.tiktok.com/@u/video/1"})
            job_id = r.json()["job_id"]
            job = api._jobs[job_id]
            await _wait_until(lambda: job.status in ("ready", "error"))

        result_path = job.result_path
        assert os.path.exists(result_path)

        d = await client.delete(f"/jobs/{job_id}")
        assert d.status_code == 204
        assert not os.path.exists(result_path)
        assert (await client.get(f"/jobs/{job_id}")).status_code == 404


# ---------------------------------------------------------------------------
# Rate limit e IP
# ---------------------------------------------------------------------------

class TestRateLimit:
    async def test_blocks_after_limit_from_same_ip(self, client, tmp_path):
        fake = tmp_path / "src" / "video.mp4"; fake.parent.mkdir(); fake.write_bytes(b"d")
        with patch("api.get_video_info", return_value=_default_info()), \
             patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            statuses = []
            for _ in range(api._RATE_LIMIT_MAX_REQUESTS + 2):
                r = await client.post(
                    "/jobs",
                    json={"url": "https://www.tiktok.com/@u/video/1"},
                    headers={"x-forwarded-for": "203.0.113.5"},
                )
                statuses.append(r.status_code)
            await asyncio.sleep(0.05)  # deja terminar los jobs en segundo plano antes de despachear el mock

        assert 429 in statuses
        # Las primeras N pasan, el resto se bloquea.
        assert statuses[0] == 202

    async def test_different_ips_independent(self, client, tmp_path):
        fake = tmp_path / "src" / "video.mp4"; fake.parent.mkdir(); fake.write_bytes(b"d")
        with patch("api.get_video_info", return_value=_default_info()), \
             patch("pipeline.download_video", return_value=str(fake)), \
             patch("pipeline.get_video_dimensions", return_value=(0, 0)):
            r1 = await client.post(
                "/jobs", json={"url": "https://www.tiktok.com/@u/video/1"},
                headers={"x-forwarded-for": "10.0.0.1"},
            )
            r2 = await client.post(
                "/jobs", json={"url": "https://www.tiktok.com/@u/video/1"},
                headers={"x-forwarded-for": "10.0.0.2"},
            )
            await asyncio.sleep(0.05)
        assert r1.status_code == 202
        assert r2.status_code == 202


class TestClientIp:
    def test_prefers_x_forwarded_for(self):
        from unittest.mock import MagicMock
        req = MagicMock()
        req.headers = {"x-forwarded-for": "1.2.3.4, 10.0.0.1"}
        assert api._client_ip(req) == "1.2.3.4"

    def test_falls_back_to_client_host(self):
        from unittest.mock import MagicMock
        req = MagicMock()
        req.headers = {}
        req.client.host = "9.9.9.9"
        assert api._client_ip(req) == "9.9.9.9"


# ---------------------------------------------------------------------------
# Salud
# ---------------------------------------------------------------------------

class TestHealth:
    async def test_health_ok(self, client):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Helpers puros
# ---------------------------------------------------------------------------

class TestJobFilesAndCleanup:
    def test_job_files_single_result(self):
        job = api.Job(id="x", result_path="/tmp/a.mp4")
        assert api._job_files(job) == ["/tmp/a.mp4"]

    def test_job_files_album(self):
        job = api.Job(id="x", result_items=[{"path": "/tmp/a.jpg", "kind": "photo"}, {"path": "/tmp/b.mp4", "kind": "video"}])
        assert api._job_files(job) == ["/tmp/a.jpg", "/tmp/b.mp4"]

    def test_job_files_empty_when_nothing_ready(self):
        job = api.Job(id="x")
        assert api._job_files(job) == []

    def test_cleanup_removes_existing_files_only(self, tmp_path):
        f = tmp_path / "a.mp4"; f.write_bytes(b"d")
        job = api.Job(id="x", result_path=str(f))
        api._cleanup_job(job)
        assert not f.exists()
        api._cleanup_job(job)  # segunda vez no debe explotar aunque ya no exista
