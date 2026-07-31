#!/bin/sh
set -e

echo "[start.sh] Запускаю PO-Token провайдер (bgutil) на 127.0.0.1:4416..."
node /opt/bgutil-provider/server/build/main.js &
BGUTIL_PID=$!

# Небольшая пауза, чтобы сервер успел подняться до первого запроса к YouTube
sleep 5

if kill -0 "$BGUTIL_PID" 2>/dev/null; then
    echo "[start.sh] PO-Token провайдер запущен (pid $BGUTIL_PID)"
else
    echo "[start.sh] ВНИМАНИЕ: PO-Token провайдер не поднялся, бот запустится без него"
fi

echo "[start.sh] Запускаю бота..."
exec python bot.py
