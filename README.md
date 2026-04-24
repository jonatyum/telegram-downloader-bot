# Telegram Video Downloader Bot

Bot de Telegram que descarga videos de TikTok, Instagram, Facebook, YouTube y X/Twitter a partir de un link y los envía de vuelta al usuario.

## Arquitectura

```
telegram-downloader-bot/
├── bot.py              # Entry point: handlers de Telegram y lógica principal
├── downloader.py       # Descarga de videos con yt-dlp
├── config.py           # Configuración centralizada desde variables de entorno
├── database.py         # Registro de usuarios en SQLite
├── rate_limiter.py     # Rate limiting por usuario (ventana deslizante)
├── bot.db              # Base de datos SQLite (generada en runtime, no en git)
├── .env                # Variables de entorno locales (no subir a git)
├── .env.example        # Plantilla de variables de entorno
├── .gitignore
├── requirements.txt
├── Dockerfile          # Para deployment en Railway/Render
└── railway.toml        # Configuración de Railway
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

## Deployment (Railway.app)

1. Push a GitHub
2. Crear proyecto en Railway → conectar repo
3. Agregar variable de entorno `BOT_TOKEN`
4. Deploy automático

## Stack

- **Python 3.11+**
- **python-telegram-bot v21** — framework async para Telegram Bot API
- **yt-dlp** — motor de descarga (fork activo de youtube-dl)
- **ffmpeg** — procesamiento de video/audio
- **Railway.app** — hosting gratuito
