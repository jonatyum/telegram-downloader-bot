# Telegram Video Downloader Bot

Descarga videos de TikTok, Instagram, Facebook, YouTube y X/Twitter a partir de un link,
por dos canales que comparten el mismo motor: un **bot de Telegram** y una **web**
(frontend estático en GitHub Pages + API en Render). Ver `CLAUDE.md` para el detalle de
la arquitectura y las decisiones de diseño.

## Arquitectura

```
telegram-downloader-bot/
├── bot.py             # Entry point del bot: handlers de Telegram, routing/presentación
├── api.py             # Entry point de la API web (FastAPI): jobs, SSE, servido de archivos
├── pipeline.py         # Orquesta una descarga ya decidida — no sabe de Telegram ni de HTTP
├── links.py            # Detección de links soportados, compartida por bot.py y api.py
├── downloader.py       # Descarga de videos con yt-dlp + ffmpeg
├── config.py           # Configuración centralizada desde variables de entorno
├── database.py         # Registro de usuarios en Postgres (Supabase) — solo el bot
├── rate_limiter.py     # Rate limiting (ventana deslizante), por user_id o por IP
├── docs/                # Frontend estático para GitHub Pages (Settings → Pages → /docs)
├── .env                # Variables de entorno locales (no subir a git)
├── .env.example        # Plantilla de variables de entorno
├── .gitignore
├── requirements.txt
├── Dockerfile           # Servicio del bot en Render
├── Dockerfile.api       # Servicio de la API web en Render (separado del bot)
└── render.yaml          # Los dos servicios de Render
```

## Flujo de datos

```
Usuario → Link → bot.py detecta plataforma
                      ↓
               downloader.py (yt-dlp)
                      ↓
           Archivo temporal en downloads/
                      ↓
         bot.py envía video a Telegram
                      ↓
         Archivo temporal eliminado
```

## Plataformas soportadas

| Plataforma | Soporte | Notas |
|---|---|---|
| TikTok | ✅ | Sin watermark cuando es posible |
| Instagram | ✅ | Reels y posts públicos |
| Facebook | ✅ | Videos públicos |
| YouTube | ✅ | Videos y Shorts |
| X / Twitter | ✅ | Videos públicos |

## Límites de Telegram

- Videos hasta **50 MB** se envían como video
- Videos entre **50–2000 MB** se envían como documento (sin preview)
- Videos más grandes no son soportados

## Setup local

```bash
# 1. Crear entorno virtual
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Configurar variables de entorno
cp .env.example .env
# Editar .env con tu token de BotFather

# 4. Ejecutar
python bot.py
```

## Canal web

```bash
uvicorn api:app --reload --port 8000
```

Sirve la misma funcionalidad sin el límite de 50 MB de Telegram — el archivo se sirve
por streaming desde disco, y el progreso se manda por SSE con porcentaje real. Para
probar el frontend local, abre `docs/index.html` y edita la constante `API_BASE` al
principio del `<script>` para que apunte a `http://localhost:8000`.

## Deployment

**Render** (`render.yaml`) despliega el bot y la API web como **dos servicios
independientes** — no comparten proceso ni RAM, así que un pico de tráfico en uno no
afecta al otro. Cada uno con su propio Dockerfile (`Dockerfile` / `Dockerfile.api`).

El frontend (`docs/`) se sirve aparte, en **GitHub Pages**: Settings → Pages → Source →
rama `main`, carpeta `/docs`. No hace falta ninguna GitHub Action. Tras el primer
deploy, hay que:
1. Poner la URL real del servicio `telegram-downloader-api` en la env var `WEB_ORIGINS`
   del backend (para que CORS deje pasar al frontend).
2. Poner la URL real de GitHub Pages en la constante `API_BASE` de `docs/index.html`.

`railway.toml` sigue en el repo como alternativa para el bot.

## Stack

- **Python 3.13**
- **python-telegram-bot v21** — framework async para Telegram Bot API
- **FastAPI + uvicorn** — API del canal web
- **yt-dlp** — motor de descarga (fork activo de youtube-dl)
- **ffmpeg** — procesamiento de video/audio
- **Postgres (Supabase)** — usuarios del bot, vía `psycopg`
- **Render** — hosting de los dos servicios · **GitHub Pages** — hosting del frontend
