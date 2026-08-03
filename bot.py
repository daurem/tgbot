import asyncio
import glob
import logging
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiohttp import web
import yt_dlp

# ==== НАСТРОЙКИ ====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задана переменная окружения BOT_TOKEN. "
        "Установи её на сервере (Render -> Environment) и не храни токен в коде."
    )
DOWNLOAD_DIR = "downloads"

LOCAL_API_URL = os.environ.get("LOCAL_BOT_API_URL")
MAX_TELEGRAM_SIZE = (2000 if LOCAL_API_URL else 50) * 1024 * 1024

FFMPEG_LOCATION = os.environ.get("FFMPEG_LOCATION", "")
if not FFMPEG_LOCATION:
    _default_win_ffmpeg = r"C:\Users\user\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin"
    if os.path.isdir(_default_win_ffmpeg):
        FFMPEG_LOCATION = _default_win_ffmpeg

FFMPEG_BIN = os.path.join(FFMPEG_LOCATION, "ffmpeg") if FFMPEG_LOCATION else "ffmpeg"

COOKIES_FILES = {
    "youtube": "cookies_youtube.txt",
    "instagram": "cookies_instagram.txt",
    "tiktok": "cookies_tiktok.txt",
}

TIKTOK_PROXY = os.environ.get("TIKTOK_PROXY", "")
YOUTUBE_PROXY = os.environ.get("YOUTUBE_PROXY", "")

# aria2c качает несколькими соединениями параллельно — заметно быстрее обычного
# скачивания yt-dlp. Требует установленного бинарника aria2c на сервере.
USE_ARIA2C = os.environ.get("USE_ARIA2C", "0") == "1"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO)

# Диагностика при старте: сразу видно в логах Render, подхватились ли куки
for _platform, _fname in COOKIES_FILES.items():
    if os.path.exists(_fname):
        logging.info(f"[cookies] {_platform}: файл {_fname} найден ({os.path.getsize(_fname)} байт)")
    else:
        logging.warning(f"[cookies] {_platform}: файл {_fname} НЕ найден рядом со скриптом")
if YOUTUBE_PROXY:
    logging.info("[proxy] YOUTUBE_PROXY задан, буду использовать прокси для YouTube")

from aiogram.client.telegram import TelegramAPIServer

if LOCAL_API_URL:
    local_server = TelegramAPIServer.from_base(LOCAL_API_URL)
    session = AiohttpSession(api=local_server)
    bot = Bot(token=BOT_TOKEN, session=session, timeout=300)
else:
    bot = Bot(token=BOT_TOKEN, timeout=300)

dp = Dispatcher()

# Храним ссылку и платформу для каждого пользователя (на случай, если понадобится)
pending_urls: dict[int, str] = {}
pending_platforms: dict[int, str] = {}

PLATFORM_PATTERNS = {
    "youtube": re.compile(r"(youtube\.com|youtu\.be)"),
    "instagram": re.compile(r"instagram\.com"),
    "tiktok": re.compile(r"tiktok\.com"),
}

def detect_platform(url: str) -> str | None:
    for name, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return name
    return None


def probe_formats(url: str, platform: str) -> list[int]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        'format': 'bestvideo+bestaudio/best',
        "extractor_args": {
            "youtube": [
                "pot_provider=http://127.0.0.1:4416"
            ]
        }
    }
    cookies_file = COOKIES_FILES.get(platform)
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
    if platform == "tiktok" and TIKTOK_PROXY:
        ydl_opts["proxy"] = TIKTOK_PROXY
    if platform == "youtube" and YOUTUBE_PROXY:
        ydl_opts["proxy"] = YOUTUBE_PROXY

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])
    heights = sorted(
        {f["height"] for f in formats if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    return heights


def build_quality_keyboard(heights: list[int]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="Лучшее доступное качество", callback_data="q_best")]]
    for h in heights:
        buttons.append([InlineKeyboardButton(text=f"{h}p", callback_data=f"q_{h}")])
    buttons.append([InlineKeyboardButton(text="Аудио — оригинал (без потерь)", callback_data="q_audio_orig")])
    buttons.append([InlineKeyboardButton(text="Аудио — MP3 320kbps", callback_data="q_audio")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def format_for_quality(quality: str) -> dict:
    if quality == "q_best":
        # Принудительно H.264 + m4a для телефонов
        return {"format": "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext=mp4]/best", "merge_output_format": "mp4"}
    if quality.startswith("q_") and quality[2:].isdigit():
        h = quality[2:]
        return {"format": f"bestvideo[height<={h}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/best[height<={h}][ext=mp4]/best", "merge_output_format": "mp4"}
    if quality == "q_audio_orig":
        return {'format': 'bestvideo+bestaudio/best'}
    if quality == "q_audio":
        return {
            'format': 'bestvideo+bestaudio/best',
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
        }
    return {'format': 'bestvideo+bestaudio/best'}


def is_audio_quality(quality: str) -> bool:
    return quality in ("q_audio", "q_audio_orig")


# Ограничим одновременно выполняющиеся скачивания (чтобы не перегружать ресурсы)
download_lock = asyncio.Semaphore(1)


@dp.message(Command("start"))
async def start(message: Message):
    text = (
        "👋 Привет! Я бот для скачивания видео с YouTube, Instagram и TikTok.\n\n"
        "📥 Просто пришли мне ссылку на видео, и я предложу выбрать качество.\n"
        "Также можно скачать аудио в оригинальном формате или в MP3 320 kbps.\n\n"
        "⚠️ Внимание: без локального Bot API сервера действует лимит Telegram на файлы до 50 МБ.\n"
        "Если видео больше, попробуйте выбрать более низкое качество."
    )
    await message.answer(text)


@dp.message(F.text)
async def handle_link(message: Message):
    url = message.text.strip()

    platform = detect_platform(url)
    if platform is None:
        await message.answer(
            "Это не похоже на ссылку YouTube, Instagram или TikTok. Пришли корректный URL."
        )
        return

    status = await message.answer("Смотрю, какие варианты качества доступны...")

    loop = asyncio.get_event_loop()
    try:
        heights = await loop.run_in_executor(None, probe_formats, url, platform)
    except Exception as e:
        logging.exception("Ошибка при получении форматов")
        note = ""
        if platform == "youtube" and "Sign in to confirm" in str(e):
            note = (
                "\n\nYouTube требует подтверждения, что это не бот. Обычно причина — "
                "IP хостинга (Render/Railway и т.п. в чёрных списках Google), а не сами куки. "
                f"Проверь, что файл {COOKIES_FILES['youtube']} свежий и лежит рядом со скриптом, "
                "и при необходимости задай YOUTUBE_PROXY (резидентный/мобильный прокси).\n"
                "Также убедись, что PO-Token провайдер запущен (bgutil) и доступен по 127.0.0.1:4416."
            )
        elif platform == "tiktok":
            note = (
                "\n\nПохоже, TikTok недоступен без прокси/VPN (это блокировка на уровне "
                "провайдера в Узбекистане, а не проблема бота). Задай TIKTOK_PROXY в коде "
                "или через переменную окружения — адрес рабочего SOCKS5/HTTP прокси или "
                "локального порта VPN-клиента."
            )
        elif platform == "instagram":
            note = (
                f"\n\nЕсли видео приватное или требует входа — положи файл "
                f"{COOKIES_FILES[platform]} рядом со скриптом (экспорт куки из "
                f"залогиненного браузера)."
            )
        await status.edit_text(f"Не удалось получить информацию о видео: {e}" + note)
        return

    # Сохраняем ссылку и платформу для пользователя
    pending_urls[message.from_user.id] = url
    pending_platforms[message.from_user.id] = platform

    await status.edit_text("Выбери качество:", reply_markup=build_quality_keyboard(heights))


@dp.callback_query(F.data.startswith("q_"))
async def handle_quality_choice(callback: CallbackQuery):
    user_id = callback.from_user.id
    url = pending_urls.get(user_id)
    platform = pending_platforms.get(user_id)

    if not url or not platform:
        await callback.answer("Ссылка устарела, пришли её заново.", show_alert=True)
        return

    await callback.answer()
    quality = callback.data

    # Сразу запускаем скачивание без лишних шагов
    await run_download_and_send(callback, quality)


async def run_download_and_send(callback: CallbackQuery, quality: str):
    user_id = callback.from_user.id
    url = pending_urls.get(user_id)
    platform = pending_platforms.get(user_id)

    if not url or not platform:
        await callback.message.edit_text("Ссылка устарела, пришли её заново.")
        return

    if download_lock.locked():
        await callback.message.edit_text("Сейчас скачивается другое видео, подожди немного...")
        return

    status_msg = None
    async with download_lock:
        status_msg = await callback.message.edit_text("Скачиваю, подожди немного...")
        await _run_download_and_send_inner(callback, status_msg, user_id, url, platform, quality)


async def _run_download_and_send_inner(
    callback: CallbackQuery,
    status_msg: Message,
    user_id: int,
    url: str,
    platform: str,
    quality: str,
):
    opts = format_for_quality(quality)

    ydl_opts = {
        **opts,
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s_{user_id}.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": [
                "pot_provider=http://127.0.0.1:4416"
            ]
        }
    }

    if FFMPEG_LOCATION:
        ydl_opts["ffmpeg_location"] = FFMPEG_LOCATION

    if USE_ARIA2C:
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = {"aria2c": ["-x", "16", "-s", "16", "-k", "1M"]}

    cookies_file = COOKIES_FILES.get(platform)
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
    if platform == "tiktok" and TIKTOK_PROXY:
        ydl_opts["proxy"] = TIKTOK_PROXY
    if platform == "youtube" and YOUTUBE_PROXY:
        ydl_opts["proxy"] = YOUTUBE_PROXY

    # Для видео – принудительно конвертируем в MP4
    if quality not in ("q_audio", "q_audio_orig"):
        ydl_opts.setdefault("postprocessors", []).append({
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        })

    def run_download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info.get("id", "video")
            title = info.get("title", "video")
            return video_id, title

    def find_downloaded_file(video_id: str) -> str | None:
        pattern = f"{DOWNLOAD_DIR}/{video_id}_{user_id}.*"
        candidates = [
            f for f in glob.glob(pattern)
            if not f.endswith((".part", ".ytdl", ".temp"))
        ]
        if not candidates:
            return None
        candidates.sort(key=os.path.getmtime, reverse=True)
        return candidates[0]

    filepath = None
    try:
        video_id, title = await asyncio.to_thread(run_download)
        filepath = find_downloaded_file(video_id)

        if not filepath:
            await status_msg.edit_text("Не удалось найти скачанный файл. Попробуй другое качество.")
            return

        file_size = os.path.getsize(filepath)

        if file_size > MAX_TELEGRAM_SIZE:
            note = (
                "" if LOCAL_API_URL else
                "\n\nЧтобы снимать этот лимит, подними локальный Bot API сервер (см. LOCAL_API_URL в коде)."
            )
            await status_msg.edit_text(
                f"Файл получился {file_size / 1024 / 1024:.1f} МБ — это больше текущего лимита "
                f"в {MAX_TELEGRAM_SIZE // 1024 // 1024} МБ. Попробуй качество пониже." + note
            )
        else:
            await status_msg.edit_text("Загружаю файл в Telegram...")
            if quality in ("q_audio", "q_audio_orig"):
                await callback.message.answer_audio(FSInputFile(filepath), title=title, request_timeout=300)
            else:
                await callback.message.answer_video(FSInputFile(filepath), caption=title, request_timeout=300)
            await status_msg.delete()

    except Exception as e:
        logging.exception("Ошибка при скачивании")
        await status_msg.edit_text(f"Произошла ошибка: {e}")

    finally:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
        # Очищаем сохранённые данные пользователя
        pending_urls.pop(user_id, None)
        pending_platforms.pop(user_id, None)


async def handle_ping(request):
    return web.Response(text="Bot is alive")


async def start_keepalive_server():
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Keepalive HTTP-сервер слушает порт {port}")


async def check_pot_provider():
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=5) as resp:
            if resp.status == 200:
                logging.info("[pot] PO-Token провайдер отвечает на 127.0.0.1:4416")
                return
    except Exception as e:
        logging.warning(f"[pot] PO-Token провайдер недоступен: {e}")


async def main():
    await start_keepalive_server()
    await check_pot_provider()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
