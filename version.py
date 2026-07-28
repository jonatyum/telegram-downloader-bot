"""
Información de versión (commit/build) para el comando /version del admin.

Permite verificar qué commit está corriendo el bot y compararlo con el head de la
rama en GitHub, para saber si estás en la última versión del repo.

Detección del commit (en orden):
  1. APP_COMMIT / GIT_COMMIT   → override manual (cualquier host)
  2. RENDER_GIT_COMMIT         → inyectada por Render en runtime
  3. git local                 → solo si hay .git y binario git (desarrollo local)
"""
import json
import os
import subprocess
import urllib.request
from datetime import datetime, timezone

# Momento de arranque del proceso actual (en Render free se reinicia en cada despertar).
STARTED_AT = datetime.now(timezone.utc)

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def _run_git(args: list[str]) -> str | None:
    try:
        r = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=5, cwd=_REPO_DIR
        )
        if r.returncode != 0:
            return None
        return r.stdout.strip() or None
    except Exception:
        return None


def get_local_version() -> dict:
    """Datos del commit/rama que el bot está ejecutando ahora."""
    env_sha = os.getenv("APP_COMMIT") or os.getenv("GIT_COMMIT")
    render_sha = os.getenv("RENDER_GIT_COMMIT")
    sha = env_sha or render_sha or _run_git(["rev-parse", "HEAD"])

    branch = (
        os.getenv("APP_BRANCH")
        or os.getenv("RENDER_GIT_BRANCH")
        or _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    )

    if env_sha:
        source = "env override"
    elif render_sha:
        source = "Render"
    elif sha:
        source = "git local"
    else:
        source = "desconocido"

    # Estas solo funcionan en local (Render slim no trae git ni .git garantizado).
    dirty = bool(_run_git(["status", "--porcelain"]))
    commit_msg = _run_git(["log", "-1", "--pretty=%s"]) if source == "git local" else None

    return {
        "sha": sha,
        "short": sha[:9] if sha else None,
        "branch": branch,
        "source": source,
        "dirty": dirty,
        "commit_msg": commit_msg,
    }


def _repo_slug() -> str | None:
    slug = os.getenv("GITHUB_REPO") or os.getenv("RENDER_GIT_REPO_SLUG")
    if slug:
        # RENDER_GIT_REPO_SLUG a veces viene como URL completa; normalizamos.
        slug = slug.strip()
    else:
        url = _run_git(["remote", "get-url", "origin"])
        slug = url.strip() if url else None
    if not slug:
        return None
    if slug.endswith(".git"):
        slug = slug[:-4]
    if slug.startswith("git@github.com:"):
        return slug.split("git@github.com:", 1)[1]
    if "github.com/" in slug:
        return slug.split("github.com/", 1)[1]
    return slug if "/" in slug else None


def _github_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "telegram-downloader-bot",
            "Accept": "application/vnd.github+json",
        },
    )
    token = os.getenv("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def check_remote(sha: str | None, branch: str | None) -> dict:
    """
    Best-effort: compara el commit en ejecución con el head de la rama en GitHub.
    status posibles: 'identical' (al día), 'behind' (hay commits más nuevos en el repo),
    'ahead' (corres algo no pusheado), 'diverged'. ok=False si no se pudo consultar.
    """
    slug = _repo_slug()
    if not slug or not branch:
        return {"ok": False, "reason": "sin repo o rama detectables"}
    try:
        head = _github_json(f"https://api.github.com/repos/{slug}/commits/{branch}")
        latest_sha = head.get("sha")
        latest_msg = ((head.get("commit") or {}).get("message") or "").split("\n")[0]
        result = {"ok": True, "latest": latest_sha, "latest_msg": latest_msg, "slug": slug}
        if not sha:
            result["status"] = "unknown"
            return result
        if sha == latest_sha:
            result["status"] = "identical"
            return result
        # base=commit corriendo, head=último en la rama → ahead_by = commits que faltan
        cmp = _github_json(f"https://api.github.com/repos/{slug}/compare/{sha}...{latest_sha}")
        result["status"] = cmp.get("status")  # ahead / behind / identical / diverged
        result["ahead_by"] = cmp.get("ahead_by")   # cuántos commits está DETRÁS el bot
        result["behind_by"] = cmp.get("behind_by")
        return result
    except Exception as e:
        return {"ok": False, "reason": str(e)[:100]}


def uptime_str() -> str:
    secs = int((datetime.now(timezone.utc) - STARTED_AT).total_seconds())
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)
