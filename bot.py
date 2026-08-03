import asyncio
import glob
import logging
import os
import re
import json
import subprocess
import html
from typing import Optional, Dict, Any, Tuple, List

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
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web
import yt_dlp

# ==== НАСТРОЙКИ ====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан!")
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

LOCAL_API_URL = os.environ.get("LOCAL_BOT_API_URL")
MAX_TELEGRAM_SIZE = (2000 if LOCAL_API_URL else 50) * 1024 * 1024

FFMPEG_LOCATION = os.environ.get("FFMPEG_LOCATION", "")
if not FFMPEG_LOCATION and os.name == "nt":
    default_win = r"C:\Users\user\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin"
    if os.path.isdir(default_win):
        FFMPEG_LOCATION = default_win
FFMPEG_BIN = os.path.join(FFMPEG_LOCATION, "ffmpeg") if FFMPEG_LOCATION else "ffmpeg"
FFPROBE_BIN = os.path.join(FFMPEG_LOCATION, "ffprobe") if FFMPEG_LOCATION else "ffprobe"

COOKIES_FILES = {
    "instagram": "cookies_instagram.txt",
    "tiktok": "cookies_tiktok.txt",
}
TIKTOK_PROXY = os.environ.get("TIKTOK_PROXY", "")
INSTAGRAM_PROXY = os.environ.get("INSTAGRAM_PROXY", "")
USE_ARIA2C = os.environ.get("USE_ARIA2C", "0") == "1"

logging.basicConfig(level=logging.INFO)

# Диагностика кук
for plat, fname in COOKIES_FILES.items():
    if os.path.exists(fname):
        logging.info(f"[cookies] {plat}: {fname} найден ({os.path.getsize(fname)} байт)")
    else:
        logging.warning(f"[cookies] {plat}: {fname} НЕ найден")

# Бот
if LOCAL_API_URL:
    local_server = TelegramAPIServer.from_base(LOCAL_API_URL)
    session = AiohttpSession(api=local_server)
    bot = Bot(token=BOT_TOKEN, session=session, timeout=300)
else:
    bot = Bot(token=BOT_TOKEN, timeout=300)
dp = Dispatcher()

# Хранилище данных пользователей
user_data: Dict[int, Dict[str, Any]] = {}

# Регулярки для Instagram и TikTok
PLATFORM_PATTERNS = {
    "instagram": re.compile(r"instagram\.com"),
    "tiktok": re.compile(r"tiktok\.com"),
}

processing_lock = asyncio.Semaphore(1)

# ==== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====

def detect_platform(url: str) -> Optional[str]:
    for name, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return name
    return None

def probe_formats(url: str, platform: str) -> Tuple[List[int], Dict, List[str]]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "bestvideo+bestaudio/best",
    }
    cookies_file = COOKIES_FILES.get(platform)
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
    if platform == "tiktok" and TIKTOK_PROXY:
        ydl_opts["proxy"] = TIKTOK_PROXY
    if platform == "instagram" and INSTAGRAM_PROXY:
        ydl_opts["proxy"] = INSTAGRAM_PROXY

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])
    heights = sorted(
        {f["height"] for f in formats if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    containers = sorted({f.get("ext") for f in formats if f.get("ext") and f.get("vcodec") != "none"})
    preferred = ["mp4", "webm"]
    containers_sorted = [c for c in preferred if c in containers] + [c for c in containers if c not in preferred]

    video_info = {
        "title": info.get("title", "Неизвестно"),
        "uploader": info.get("uploader", "Неизвестно"),
        "duration": info.get("duration", 0),
        "view_count": info.get("view_count", 0),
    }
    return heights, video_info, containers_sorted

def build_quality_keyboard(heights: List[int]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="Лучшее доступное качество", callback_data="q_best")]]
    for h in heights:
        buttons.append([InlineKeyboardButton(text=f"{h}p", callback_data=f"q_{h}")])
    buttons.append([InlineKeyboardButton(text="Аудио — оригинал", callback_data="q_audio_orig")])
    buttons.append([InlineKeyboardButton(text="Аудио — MP3 320kbps", callback_data="q_audio")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_format_keyboard(containers: List[str]) -> InlineKeyboardMarkup:
    buttons = []
    for c in containers:
        label = c.upper()
        if c == "mp4":
            label += " (H.264)"
        elif c == "webm":
            label += " (VP9)"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"fmt_{c}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def format_duration(seconds: int) -> str:
    if not seconds:
        return "Неизвестно"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def get_video_info(file_path: str) -> Dict:
    cmd = [
        FFPROBE_BIN,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        file_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video_stream = next((s for s in streams if s["codec_type"] == "video"), None)
        audio_stream = next((s for s in streams if s["codec_type"] == "audio"), None)
        fmt = data.get("format", {})
        info = {
            "size": os.path.getsize(file_path),
            "duration": float(fmt.get("duration", 0)),
            "bitrate": int(fmt.get("bit_rate", 0)) if fmt.get("bit_rate") else None,
        }
        if video_stream:
            info.update({
                "width": int(video_stream.get("width", 0)),
                "height": int(video_stream.get("height", 0)),
                "video_codec": video_stream.get("codec_name", "unknown"),
            })
        if audio_stream:
            info["audio_codec"] = audio_stream.get("codec_name", "unknown")
        return info
    except Exception as e:
        logging.error(f"ffprobe error: {e}")
        return {"size": os.path.getsize(file_path), "error": str(e)}

async def convert_video_format(input_path: str, target_ext: str) -> Optional[str]:
    """
    Конвертирует видео в указанный контейнер с максимальной совместимостью.
    Возвращает путь к новому файлу (с суффиксом _converted).
    """
    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_converted.{target_ext}"
    if os.path.exists(output_path):
        os.remove(output_path)

    if target_ext == "mp4":
        vcodec = "libx264"
        acodec = "aac"
        extra_args = ["-profile:v", "baseline", "-level", "3.0", "-movflags", "+faststart"]
    elif target_ext == "webm":
        vcodec = "libvpx-vp9"
        acodec = "libopus"
        extra_args = []
    else:
        return None

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i", input_path,
        "-c:v", vcodec,
        "-c:a", acodec,
        "-preset", "ultrafast",
        "-threads", "1",
    ] + extra_args + [output_path]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path):
            logging.error(f"convert error: {stderr.decode(errors='ignore')}")
            return None
        return output_path
    except Exception as e:
        logging.error(f"convert exception: {e}")
        return None

def build_ydl_opts(quality: str, container: str, platform: str, user_id: int) -> Dict:
    if quality == "q_best":
        fmt_str = f"bestvideo[ext={container}]+bestaudio[ext=m4a]/best[ext={container}]/best"
    elif quality.startswith("q_") and quality[2:].isdigit():
        h = quality[2:]
        fmt_str = f"bestvideo[height<={h}][ext={container}]+bestaudio[ext=m4a]/best[height<={h}][ext={container}]/best"
    elif quality == "q_audio_orig":
        fmt_str = "bestaudio/best"
    elif quality == "q_audio":
        fmt_str = "bestaudio/best"
    else:
        fmt_str = "bestvideo+bestaudio/best"

    opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s_{user_id}.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    if quality in ("q_audio", "q_audio_orig"):
        opts["format"] = fmt_str
        if quality == "q_audio":
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }]
    else:
        opts["format"] = fmt_str
        opts["merge_output_format"] = container
        opts["postprocessors"] = [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": container,
        }]

    if FFMPEG_LOCATION:
        opts["ffmpeg_location"] = FFMPEG_LOCATION
    if USE_ARIA2C:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = {"aria2c": ["-x", "16", "-s", "16", "-k", "1M"]}
    cookies_file = COOKIES_FILES.get(platform)
    if cookies_file and os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file
    if platform == "tiktok" and TIKTOK_PROXY:
        opts["proxy"] = TIKTOK_PROXY
    if platform == "instagram" and INSTAGRAM_PROXY:
        opts["proxy"] = INSTAGRAM_PROXY

    return opts

async def download_video(url: str, platform: str, quality: str, container: str, user_id: int) -> Tuple[Optional[str], Optional[str]]:
    opts = build_ydl_opts(quality, container, platform, user_id)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info.get("id", "video")
            title = info.get("title", "video")
            pattern = f"{DOWNLOAD_DIR}/{video_id}_{user_id}.*"
            candidates = glob.glob(pattern)
            valid = [f for f in candidates if not f.endswith((".part", ".ytdl", ".temp"))]
            if valid:
                valid.sort(key=os.path.getmtime, reverse=True)
                return valid[0], title
            return None, title
    except Exception as e:
        logging.exception("download_video error")
        return None, None

# ==== ОБРАБОТЧИКИ ====

@dp.message(Command("start"))
async def start(message: Message):
    text = (
        "👋 <b>Привет! Я скачиваю видео с Instagram и TikTok.</b>\n\n"
        "📌 <b>Как это работает:</b>\n"
        "1. Отправь мне ссылку на видео с Instagram или TikTok.\n"
        "2. Я покажу доступные <b>качества</b> – выбери нужное.\n"
        "3. Затем выбери <b>формат</b> (контейнер) – MP4, WEBM и другие.\n"
        "4. Я скачаю видео и пришлю его с <b>полной информацией</b>:\n"
        "   размер, длительность, разрешение, кодеки и битрейт.\n\n"
        "⚠️ <i>Лимит Telegram на файлы – 50 МБ (без локального Bot API).</i>\n"
        "Если видео приватное – добавь файл cookies_instagram.txt или cookies_tiktok.txt рядом со скриптом."
    )
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text)
async def handle_link(message: Message):
    url = message.text.strip()
    platform = detect_platform(url)
    if platform is None:
        await message.answer("❌ Поддерживаются только ссылки на Instagram и TikTok.")
        return

    status = await message.answer("🔍 Получаю информацию о видео...")

    try:
        heights, video_info, containers = await asyncio.to_thread(probe_formats, url, platform)
    except Exception as e:
        logging.exception("probe_formats error")
        note = ""
        if "login" in str(e).lower() or "cookie" in str(e).lower():
            note = f"\n\nВозможно, видео приватное. Добавь файл {COOKIES_FILES.get(platform)} (экспорт кук из браузера)."
        await status.edit_text(f"❌ Не удалось получить информацию: {e}" + note)
        return

    user_data[message.from_user.id] = {
        "url": url,
        "platform": platform,
        "heights": heights,
        "containers": containers,
        "video_info": video_info,
        "state": "awaiting_quality"
    }

    title = html.escape(video_info['title'])
    uploader = html.escape(video_info['uploader'])
    heights_str = ', '.join([f'{h}p' for h in heights])
    info_text = (
        f"📹 <b>{title}</b>\n"
        f"👤 Автор: {uploader}\n"
        f"⏱ Длительность: {format_duration(video_info['duration'])}\n"
        f"👁 Просмотров: {video_info['view_count']}\n"
        f"📐 Доступные разрешения: {heights_str}\n"
    )
    if containers:
        info_text += f"📦 Контейнеры: {', '.join(containers)}"

    await status.edit_text(
        info_text + "\n\nВыбери качество:",
        reply_markup=build_quality_keyboard(heights),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("q_"))
async def handle_quality_choice(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") != "awaiting_quality":
        await callback.answer("Сессия устарела, пришли ссылку заново.", show_alert=True)
        return

    quality = callback.data
    await callback.answer()
    data["quality"] = quality
    data["state"] = "awaiting_format"

    containers = data.get("containers", [])
    if not containers:
        if quality in ("q_audio", "q_audio_orig"):
            data["container"] = "mp3" if quality == "q_audio" else "m4a"
            await download_and_send(callback, data)
        else:
            await callback.message.edit_text("❌ Нет доступных контейнеров для видео.")
        return

    await callback.message.edit_text(
        "Выбери формат (контейнер):",
        reply_markup=build_format_keyboard(containers)
    )

@dp.callback_query(F.data.startswith("fmt_"))
async def handle_format_choice(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") != "awaiting_format":
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    container = callback.data[4:]
    await callback.answer()
    data["container"] = container
    await download_and_send(callback, data)

async def download_and_send(callback: CallbackQuery, data: dict):
    user_id = callback.from_user.id
    quality = data.get("quality")
    container = data.get("container", "mp4")
    url = data["url"]
    platform = data["platform"]

    if processing_lock.locked():
        await callback.message.edit_text("⏳ Обрабатывается другое видео, подожди...")
        return

    status = await callback.message.edit_text("📥 Скачиваю видео...")
    async with processing_lock:
        filepath, title = await download_video(url, platform, quality, container, user_id)
        if not filepath or not os.path.exists(filepath):
            await status.edit_text("❌ Не удалось скачать видео. Попробуй другое качество/формат.")
            user_data.pop(user_id, None)
            return

        # Принудительное перекодирование для видео (не для аудио)
        if quality not in ("q_audio", "q_audio_orig") and container in ("mp4", "webm"):
            await status.edit_text(f"🔄 Конвертирую в {container.upper()}...")
            new_path = await convert_video_format(filepath, container)
            if new_path and os.path.exists(new_path):
                os.remove(filepath)
                filepath = new_path
            else:
                await status.edit_text("⚠️ Не удалось конвертировать, отправляю в исходном формате.")

        file_info = get_video_info(filepath)
        size_mb = file_info.get("size", 0) / (1024 * 1024)
        duration = file_info.get("duration", 0)
        width = file_info.get("width", 0)
        height = file_info.get("height", 0)
        vcodec = file_info.get("video_codec", "неизвестно")
        acodec = file_info.get("audio_codec", "неизвестно")
        bitrate = file_info.get("bitrate", 0)
        bitrate_str = f"{bitrate//1000} kbps" if bitrate else "неизвестно"

        title_esc = html.escape(title)
        info_text = (
            f"📄 <b>{title_esc}</b>\n"
            f"📦 Размер: {size_mb:.2f} МБ\n"
            f"⏱ Длительность: {format_duration(int(duration))}\n"
            f"🖥 Разрешение: {width}x{height}\n"
            f"🎞 Видео-кодек: {vcodec}\n"
            f"🎵 Аудио-кодек: {acodec}\n"
            f"📊 Битрейт: {bitrate_str}\n"
        )

        if file_info.get("size", 0) > MAX_TELEGRAM_SIZE:
            note = "" if LOCAL_API_URL else "\n\n⚠️ Лимит Telegram 50 МБ."
            await status.edit_text(f"❌ Файл слишком большой ({size_mb:.1f} МБ).{note}")
            os.remove(filepath)
            user_data.pop(user_id, None)
            return

        await status.edit_text("📤 Загружаю в Telegram...")
        try:
            if quality in ("q_audio", "q_audio_orig"):
                await callback.message.answer_audio(
                    FSInputFile(filepath),
                    title=title,
                    caption=info_text,
                    request_timeout=300,
                    parse_mode="HTML"
                )
            else:
                await callback.message.answer_video(
                    FSInputFile(filepath),
                    caption=info_text,
                    supports_streaming=True,
                    request_timeout=300,
                    parse_mode="HTML"
                )
        except Exception as e:
            logging.exception("Ошибка при отправке")
            await status.edit_text(f"❌ Ошибка при отправке: {e}")
        else:
            await status.delete()

        os.remove(filepath)
        user_data.pop(user_id, None)

# ==== KEEPALIVE ====

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
