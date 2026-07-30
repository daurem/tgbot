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
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8737695772:AAE_-i3wnlvoOE3CCcKdx2uqJSHKwsP9eB8")
DOWNLOAD_DIR = "downloads"

LOCAL_API_URL = os.environ.get("LOCAL_BOT_API_URL")
MAX_TELEGRAM_SIZE = (2000 if LOCAL_API_URL else 50) * 1024 * 1024

FFMPEG_LOCATION = os.environ.get("FFMPEG_LOCATION", "")
if not FFMPEG_LOCATION:
    _default_win_ffmpeg = r"C:\Users\user\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin"
    if os.path.isdir(_default_win_ffmpeg):
        FFMPEG_LOCATION = _default_win_ffmpeg

COOKIES_FILES = {
    "youtube": "cookies_youtube.txt",
    "instagram": "cookies_instagram.txt",
    "tiktok": "cookies_tiktok.txt",
}

TIKTOK_PROXY = os.environ.get("TIKTOK_PROXY", "")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO)

from aiogram.client.telegram import TelegramAPIServer

if LOCAL_API_URL:
    local_server = TelegramAPIServer.from_base(LOCAL_API_URL)
    session = AiohttpSession(api=local_server)
    bot = Bot(token=BOT_TOKEN, session=session)
else:
    bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()

pending_urls: dict[int, str] = {}
pending_platforms: dict[int, str] = {}
pending_formats: dict[int, list[int]] = {}

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
        "extractor_args": {
            "youtube": {"player_client": ["android", "web"]}
        }
    }
    cookies_file = COOKIES_FILES.get(platform)
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
    if platform == "tiktok" and TIKTOK_PROXY:
        ydl_opts["proxy"] = TIKTOK_PROXY

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
        return {"format": "bestaudio/best"}
    if quality == "q_audio":
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
        }
    return {"format": "bestvideo+bestaudio/best"}


@dp.message(Command("start"))
async def start(message: Message):
    limit_note = (
        "Лимит на размер файла снят (локальный сервер, до 2000 МБ)."
        if LOCAL_API_URL
        else "Внимание: без локального Bot API сервера действует лимит Telegram в 50 МБ на файл."
    )
    await message.answer(
        "Привет! Пришли ссылку на видео с YouTube, Instagram или TikTok, "
        "и я покажу доступные варианты скачивания.\n\n" + limit_note
    )


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
        if platform == "tiktok":
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

    pending_urls[message.from_user.id] = url
    pending_platforms[message.from_user.id] = platform
    pending_formats[message.from_user.id] = heights

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
    status_msg = await callback.message.edit_text("Скачиваю, подожди немного...")

    quality = callback.data
    opts = format_for_quality(quality)

    ydl_opts = {
        **opts,
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s_{user_id}.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {"player_client": ["android", "web"]}
        }
    }

    if FFMPEG_LOCATION:
        ydl_opts["ffmpeg_location"] = FFMPEG_LOCATION

    cookies_file = COOKIES_FILES.get(platform)
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
    if platform == "tiktok" and TIKTOK_PROXY:
        ydl_opts["proxy"] = TIKTOK_PROXY

    # Для видео – принудительно конвертируем в MP4 (для телефонов)
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
                await callback.message.answer_audio(FSInputFile(filepath), title=title)
            else:
                await callback.message.answer_video(FSInputFile(filepath), caption=title)
            await status_msg.delete()

        os.remove(filepath)

    except Exception as e:
        logging.exception("Ошибка при скачивании")
        await status_msg.edit_text(f"Произошла ошибка: {e}")

    finally:
        pending_urls.pop(user_id, None)
        pending_platforms.pop(user_id, None)
        pending_formats.pop(user_id, None)


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


async def main():
    await start_keepalive_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
