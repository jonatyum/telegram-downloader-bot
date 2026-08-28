FROM python:3.13-slim

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Runtime de JavaScript para yt-dlp. No es opcional para YouTube: sin un runtime JS,
# yt-dlp cae al set de clientes "jsless" (solo `visionos`) y la extracción queda
# deprecada y con formatos faltantes. Y en cuanto se usan cookies, yt-dlp pasa a los
# clientes autenticados (web_embedded, tv_downgraded, web), que TODOS requieren JS:
# sin esto, las cookies devolverían solo storyboards en vez de video.
# Se copia el binario de la imagen oficial (pinneada) en vez de bajarlo con curl.
# OJO con la versión: yt-dlp exige deno >= 2.3.0 (MIN_SUPPORTED_VERSION en
# utils/_jsruntime.py). Con una anterior lo detecta pero lo marca no soportado y
# lo ignora en silencio, cayendo otra vez al modo sin runtime JS.
COPY --from=denoland/deno:bin-2.9.6 /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
