FROM python:3.12-slim

# ffmpeg — для склейки видео/аудио
# git, curl, gnupg — чтобы поставить Node.js и склонировать POT-провайдер
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg git curl gnupg ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- PO-Token провайдер (обходит "Sign in to confirm you're not a bot") ---
# Собираем Node.js-сервер один раз при билде образа
RUN git clone --single-branch --branch 1.3.1 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-provider \
    && cd /opt/bgutil-provider/server \
    && npm ci \
    && npx tsc

WORKDIR /app

# Python-зависимости (включая yt-dlp и плагин bgutil-ytdlp-pot-provider)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем все файлы проекта (включая cookies_youtube.txt и bot.py)
COPY . .

RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]
