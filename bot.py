import asyncio
import glob
import logging
import os
import re
import json
import subprocess
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
    Video,
    Document,
)
from aiogram.client.telegram import TelegramAPIServer
from aiohttp import web
import yt_dlp

# ==== НАСТРОЙКИ ====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан!")
DOWNLOAD_DIR = "downloads"
SUBTITLES_DIR = "subtitles"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(SUBTITLES_DIR, exist_ok=True)

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
    "youtube": "cookies_youtube.txt",
    "instagram": "cookies_instagram.txt",
    "tiktok": "cookies_tiktok.txt",
}
TIKTOK_PROXY = os.environ.get("TIKTOK_PROXY", "")
YOUTUBE_PROXY = os.environ.get("YOUTUBE_PROXY", "")
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

# Регулярки платформ
PLATFORM_PATTERNS = {
    "youtube": re.compile(r"(youtube\.com|youtu\.be)"),
    "instagram": re.compile(r"instagram\.com"),
    "tiktok": re.compile(r"tiktok\.com"),
}

# Стили субтитров
SUB_STYLES = {
    "classic": {
        "name": "Классический",
        "style": "FontName=Arial,FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Bold=0,Italic=0"
    },
    "contrast": {
        "name": "Контрастный",
        "style": "FontName=Arial,FontSize=24,PrimaryColour=&H00FFFF00,OutlineColour=&H00000080,Bold=1,Italic=0"
    },
    "minimal": {
        "name": "Минималистичный",
        "style": "FontName=Helvetica,FontSize=18,PrimaryColour=&H00000000,BackColour=&H66FFFFFF,OutlineColour=&H00000000,Bold=0,Italic=0"
    }
}

processing_lock = asyncio.Semaphore(1)

# ==== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====

def detect_platform(url: str) -> Optional[str]:
    for name, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return name
    return None

def probe_formats(url: str, platform: str) -> Tuple[List[int], Dict, List[str]]:
    """
    Возвращает (heights, video_info, available_containers)
    available_containers – список доступных расширений (mp4, webm, ...)
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "format": "bestvideo+bestaudio/best",
        "extractor_args": {"youtube": ["pot_provider=http://127.0.0.1:4416"]},
        "writesubtitles": True,
        "subtitleslangs": ["all"],
        "writeautomaticsub": True,
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
    containers = sorted({f.get("ext") for f in formats if f.get("ext") and f.get("vcodec") != "none"})
    # Приоритет: mp4, webm, остальные
    preferred = ["mp4", "webm"]
    containers_sorted = [c for c in preferred if c in containers] + [c for c in containers if c not in preferred]

    subtitles = info.get("subtitles", {}) or info.get("automatic_captions", {})
    langs = list(subtitles.keys()) if subtitles else []

    video_info = {
        "title": info.get("title", "Неизвестно"),
        "uploader": info.get("uploader", "Неизвестно"),
        "duration": info.get("duration", 0),
        "view_count": info.get("view_count", 0),
        "subtitles": langs,
        "thumbnail": info.get("thumbnail"),
    }
    return heights, video_info, containers_sorted

def build_quality_keyboard(heights: List[int]) -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text="Лучшее доступное качество", callback_data="q_best")]]
    for h in heights:
        buttons.append([InlineKeyboardButton(text=f"{h}p", callback_data=f"q_{h}")])
    buttons.append([InlineKeyboardButton(text="Аудио — оригинал (без потерь)", callback_data="q_audio_orig")])
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
    # Кнопка "Оригинал" – если есть отдельный формат, но мы просто предлагаем контейнеры
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_subtitle_lang_keyboard(langs: List[str]) -> InlineKeyboardMarkup:
    langs = sorted(set(langs))
    buttons = [[InlineKeyboardButton(text=lang.upper(), callback_data=f"sub_lang_{lang}")] for lang in langs]
    buttons.append([InlineKeyboardButton(text="❌ Пропустить субтитры", callback_data="sub_skip")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_style_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for key, style in SUB_STYLES.items():
        buttons.append([InlineKeyboardButton(text=style["name"], callback_data=f"style_{key}")])
    buttons.append([InlineKeyboardButton(text="❌ Без субтитров", callback_data="sub_skip")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_user_video_actions_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="ℹ️ Информация о видео", callback_data="user_info")],
        [InlineKeyboardButton(text="🔄 Изменить формат", callback_data="user_convert")],
        [InlineKeyboardButton(text="🎬 Добавить субтитры", callback_data="user_subs")],
        [InlineKeyboardButton(text="⏩ Отправить как есть", callback_data="user_send")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def build_convert_format_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="MP4 (H.264)", callback_data="conv_mp4")],
        [InlineKeyboardButton(text="WEBM (VP9)", callback_data="conv_webm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="conv_cancel")],
    ]
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
    """Использует ffprobe для получения информации о файле."""
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

async def download_subtitles_from_youtube(url: str, lang: str) -> Optional[str]:
    """Скачивает субтитры с YouTube в формате .srt."""
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "writesubtitles": True,
        "subtitleslangs": [lang],
        "writeautomaticsub": True,
        "subtitlesformat": "srt",
        "outtmpl": os.path.join(SUBTITLES_DIR, f"sub_%(id)s_{lang}.%(ext)s"),
        "extractor_args": {"youtube": ["pot_provider=http://127.0.0.1:4416"]}
    }
    cookies_file = COOKIES_FILES.get("youtube")
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
    if YOUTUBE_PROXY:
        ydl_opts["proxy"] = YOUTUBE_PROXY

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        pattern = os.path.join(SUBTITLES_DIR, f"sub_*_{lang}.srt")
        files = glob.glob(pattern)
        if files:
            files.sort(key=os.path.getmtime, reverse=True)
            return files[0]
        return None
    except Exception as e:
        logging.error(f"download_subtitles error: {e}")
        return None

async def apply_subtitles(video_path: str, subs_path: str, style_key: str) -> Optional[str]:
    """Накладывает субтитры с заданным стилем."""
    if not os.path.exists(subs_path):
        return None
    style = SUB_STYLES.get(style_key)
    if not style:
        return None

    base, ext = os.path.splitext(video_path)
    output_path = f"{base}_with_subs_{style_key}.mp4"

    subs_path_escaped = subs_path.replace("\\", "/")
    filter_str = f"subtitles='{subs_path_escaped}':force_style='{style['style']}'"

    cmd = [
        FFMPEG_BIN,
        "-y",
        "-i", video_path,
        "-vf", filter_str,
        "-c:a", "copy",
        "-preset", "ultrafast",
        "-threads", "1",
        output_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path):
            logging.error(f"ffmpeg sub error: {stderr.decode(errors='ignore')}")
            return None
        return output_path
    except Exception as e:
        logging.error(f"apply_subtitles exception: {e}")
        return None

async def convert_video_format(input_path: str, target_ext: str) -> Optional[str]:
    """Конвертирует видео в указанный контейнер (mp4 или webm)."""
    base, _ = os.path.splitext(input_path)
    output_path = f"{base}.{target_ext}"

    # Определяем кодек в зависимости от расширения
    if target_ext == "mp4":
        vcodec = "libx264"
        acodec = "aac"
    elif target_ext == "webm":
        vcodec = "libvpx-vp9"
        acodec = "libopus"
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
        output_path
    ]
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
    """Формирует опции для yt-dlp с учётом качества и контейнера."""
    # Базовый формат-строка
    if quality == "q_best":
        fmt_str = f"bestvideo[ext={container}][vcodec^=avc1]+bestaudio[ext=m4a]/best[ext={container}]/best"
    elif quality.startswith("q_") and quality[2:].isdigit():
        h = quality[2:]
        fmt_str = f"bestvideo[height<={h}][ext={container}][vcodec^=avc1]+bestaudio[ext=m4a]/best[height<={h}][ext={container}]/best"
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
        "extractor_args": {"youtube": ["pot_provider=http://127.0.0.1:4416"]}
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
        # Конвертируем в указанный контейнер, если ещё не тот
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
    if platform == "youtube" and YOUTUBE_PROXY:
        opts["proxy"] = YOUTUBE_PROXY

    return opts

async def download_video(url: str, platform: str, quality: str, container: str, user_id: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Скачивает видео, возвращает путь и название.
    """
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
        "👋 Привет! Я умею:\n"
        "1. Скачивать видео с YouTube, Instagram, TikTok по ссылке.\n"
        "2. Добавлять субтитры (из YouTube или загрузить свой файл).\n"
        "3. Конвертировать видео в разные форматы (MP4, WEBM).\n"
        "4. Показывать подробную информацию о видео.\n\n"
        "📌 Отправь ссылку или видеофайл, и я помогу!"
    )
    await message.answer(text)

# --- Обработка ссылок ---

@dp.message(F.text)
async def handle_link(message: Message):
    url = message.text.strip()
    platform = detect_platform(url)
    if platform is None:
        await message.answer("Это не похоже на ссылку YouTube, Instagram или TikTok.")
        return

    status = await message.answer("🔍 Получаю информацию о видео...")

    try:
        heights, video_info, containers = await asyncio.to_thread(probe_formats, url, platform)
    except Exception as e:
        logging.exception("probe_formats error")
        note = ""
        if platform == "youtube" and "Sign in to confirm" in str(e):
            note = "\n\nYouTube требует подтверждения (IP в чёрном списке). Попробуй позже или используй прокси."
        elif platform == "tiktok":
            note = "\n\nTikTok может быть заблокирован в вашем регионе. Используй TIKTOK_PROXY."
        await status.edit_text(f"❌ Не удалось получить информацию: {e}" + note)
        return

    # Сохраняем данные
    user_data[message.from_user.id] = {
        "url": url,
        "platform": platform,
        "heights": heights,
        "containers": containers,
        "video_info": video_info,
        "state": "awaiting_quality"
    }

    # Показываем информацию + выбор качества
    info_text = (
        f"📹 **{video_info['title']}**\n"
        f"👤 Автор: {video_info['uploader']}\n"
        f"⏱ Длительность: {format_duration(video_info['duration'])}\n"
        f"👁 Просмотров: {video_info['view_count']}\n"
        f"📐 Доступные разрешения: {', '.join([f'{h}p' for h in heights])}\n"
    )
    if containers:
        info_text += f"📦 Контейнеры: {', '.join(containers)}\n"
    if video_info.get('subtitles'):
        info_text += f"🔤 Субтитры: {', '.join(video_info['subtitles'])}"

    await status.edit_text(
        info_text + "\n\nВыбери качество:",
        reply_markup=build_quality_keyboard(heights),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("q_"))
async def handle_quality_choice(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") != "awaiting_quality":
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    quality = callback.data
    await callback.answer()
    data["quality"] = quality
    data["state"] = "awaiting_format"

    # Переходим к выбору формата
    containers = data.get("containers", [])
    if not containers:
        # Если контейнеров нет (например, аудио), сразу переходим к субтитрам
        if quality in ("q_audio", "q_audio_orig"):
            data["container"] = "mp3" if quality == "q_audio" else "m4a"
            data["state"] = "awaiting_sub_for_link"
            if data["platform"] == "youtube" and data["video_info"].get("subtitles"):
                await callback.message.edit_text(
                    "Выбери язык субтитров или пропусти:",
                    reply_markup=build_subtitle_lang_keyboard(data["video_info"]["subtitles"])
                )
            else:
                # Нет субтитров – сразу скачиваем
                await download_and_send_link(callback, data)
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

    container = callback.data[4:]  # убираем "fmt_"
    await callback.answer()
    data["container"] = container
    data["state"] = "awaiting_sub_for_link"

    # Если YouTube и есть субтитры – предложить
    if data["platform"] == "youtube" and data["video_info"].get("subtitles"):
        await callback.message.edit_text(
            "Выбери язык субтитров или пропусти:",
            reply_markup=build_subtitle_lang_keyboard(data["video_info"]["subtitles"])
        )
    else:
        await download_and_send_link(callback, data)

@dp.callback_query(F.data.startswith("sub_lang_"))
async def handle_sub_lang_link(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") != "awaiting_sub_for_link":
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    lang = callback.data[9:]  # убираем "sub_lang_"
    await callback.answer()
    data["sub_lang"] = lang
    data["state"] = "sub_downloading"
    await callback.message.edit_text("⬇️ Скачиваю субтитры...")

    # Скачиваем субтитры
    subs_path = await download_subtitles_from_youtube(data["url"], lang)
    if subs_path:
        data["subs_file"] = subs_path
        data["state"] = "awaiting_style"
        await callback.message.edit_text(
            "🎨 Выбери стиль субтитров:",
            reply_markup=build_style_keyboard()
        )
    else:
        await callback.message.edit_text("❌ Не удалось скачать субтитры. Отправляю без них.")
        data.pop("sub_lang", None)
        await download_and_send_link(callback, data)

@dp.callback_query(F.data == "sub_skip")
async def handle_sub_skip_link(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data:
        await callback.answer("Ошибка.", show_alert=True)
        return
    await callback.answer()
    # Если мы в состоянии выбора стиля (для пользовательских видео тоже может быть), то просто пропускаем
    if data.get("state") == "awaiting_style":
        data["state"] = "skipped_style"
    elif data.get("state") == "awaiting_sub_for_link":
        data["state"] = "skipped_sub"
    await download_and_send_link(callback, data)

async def download_and_send_link(callback: CallbackQuery, data: dict):
    """Завершающая стадия: скачивание и отправка с информацией."""
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

        # Если выбрали стиль – применяем субтитры
        if data.get("subs_file") and data.get("state") == "awaiting_style":
            style_key = data.get("style_key")
            if style_key:
                await status.edit_text("🎞 Накладываю субтитры...")
                new_path = await apply_subtitles(filepath, data["subs_file"], style_key)
                if new_path and os.path.exists(new_path):
                    os.remove(filepath)
                    filepath = new_path
                else:
                    await status.edit_text("⚠️ Не удалось наложить субтитры, отправляю без них.")
            # Удаляем файл субтитров
            if data.get("subs_file") and os.path.exists(data["subs_file"]):
                os.remove(data["subs_file"])

        # Получаем информацию о готовом файле
        file_info = get_video_info(filepath)
        size_mb = file_info.get("size", 0) / (1024 * 1024)
        duration = file_info.get("duration", 0)
        width = file_info.get("width", 0)
        height = file_info.get("height", 0)
        vcodec = file_info.get("video_codec", "неизвестно")
        acodec = file_info.get("audio_codec", "неизвестно")
        bitrate = file_info.get("bitrate", 0)
        bitrate_str = f"{bitrate//1000} kbps" if bitrate else "неизвестно"

        info_text = (
            f"📄 **{title}**\n"
            f"📦 Размер: {size_mb:.2f} МБ\n"
            f"⏱ Длительность: {format_duration(int(duration))}\n"
            f"🖥 Разрешение: {width}x{height}\n"
            f"🎞 Видео-кодек: {vcodec}\n"
            f"🎵 Аудио-кодек: {acodec}\n"
            f"📊 Битрейт: {bitrate_str}\n"
        )

        if file_info.get("size", 0) > MAX_TELEGRAM_SIZE:
            note = "" if LOCAL_API_URL else "\n\n⚠️ Лимит Telegram 50 МБ. Используй локальный Bot API для снятия лимита."
            await status.edit_text(f"❌ Файл слишком большой ({size_mb:.1f} МБ).{note}")
            os.remove(filepath)
            user_data.pop(user_id, None)
            return

        await status.edit_text("📤 Загружаю в Telegram...")
        if quality in ("q_audio", "q_audio_orig"):
            await callback.message.answer_audio(FSInputFile(filepath), title=title, caption=info_text, request_timeout=300, parse_mode="Markdown")
        else:
            await callback.message.answer_video(FSInputFile(filepath), caption=info_text, request_timeout=300, parse_mode="Markdown")
        await status.delete()
        os.remove(filepath)
        user_data.pop(user_id, None)

# Обработчик выбора стиля для ссылок (после загрузки субтитров)
@dp.callback_query(F.data.startswith("style_"))
async def handle_style_choice(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") not in ("awaiting_style", "awaiting_style_user"):
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    style_key = callback.data[6:]  # убираем "style_"
    await callback.answer()
    data["style_key"] = style_key
    data["state"] = "style_chosen"

    # Если это ссылка – запускаем скачивание
    if data.get("url"):
        await download_and_send_link(callback, data)
    else:
        # Это пользовательское видео
        await apply_subs_and_send_user_video(callback, data)

# --- Обработка загруженных видео ---

@dp.message(F.video | F.document)
async def handle_user_video(message: Message):
    user_id = message.from_user.id
    video = message.video
    doc = message.document
    if doc and not (doc.mime_type and doc.mime_type.startswith("video/")):
        await message.answer("Пожалуйста, отправьте видеофайл.")
        return

    file = video or doc
    if not file:
        await message.answer("Не удалось распознать файл.")
        return

    status = await message.answer("⬇️ Скачиваю ваш файл...")
    file_path = os.path.join(DOWNLOAD_DIR, f"user_{user_id}_{file.file_id}.mp4")
    try:
        await bot.download(file, destination=file_path)
    except Exception as e:
        logging.exception("Download user file error")
        await status.edit_text(f"❌ Ошибка: {e}")
        return

    # Сохраняем в user_data
    user_data[user_id] = {
        "video_file": file_path,
        "title": file.file_name or "video",
        "state": "awaiting_user_action",
        "is_user_video": True,
        "file_info": get_video_info(file_path)
    }

    await status.edit_text(
        "📥 Файл загружен.\nВыбери действие:",
        reply_markup=build_user_video_actions_keyboard()
    )

@dp.callback_query(F.data == "user_info")
async def show_user_video_info(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") != "awaiting_user_action":
        await callback.answer("Нет видео.", show_alert=True)
        return
    await callback.answer()
    info = data.get("file_info", {})
    size_mb = info.get("size", 0) / (1024 * 1024)
    duration = info.get("duration", 0)
    width = info.get("width", 0)
    height = info.get("height", 0)
    vcodec = info.get("video_codec", "неизвестно")
    acodec = info.get("audio_codec", "неизвестно")
    bitrate = info.get("bitrate", 0)
    bitrate_str = f"{bitrate//1000} kbps" if bitrate else "неизвестно"

    info_text = (
        f"ℹ️ **Информация о видео**\n"
        f"📄 Имя: {data.get('title', 'неизвестно')}\n"
        f"📦 Размер: {size_mb:.2f} МБ\n"
        f"⏱ Длительность: {format_duration(int(duration))}\n"
        f"🖥 Разрешение: {width}x{height}\n"
        f"🎞 Видео-кодек: {vcodec}\n"
        f"🎵 Аудио-кодек: {acodec}\n"
        f"📊 Битрейт: {bitrate_str}\n"
    )
    await callback.message.edit_text(info_text, parse_mode="Markdown")

@dp.callback_query(F.data == "user_convert")
async def start_user_convert(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") != "awaiting_user_action":
        await callback.answer("Нет видео.", show_alert=True)
        return
    await callback.answer()
    data["state"] = "awaiting_convert_format"
    await callback.message.edit_text(
        "Выбери целевой формат:",
        reply_markup=build_convert_format_keyboard()
    )

@dp.callback_query(F.data.startswith("conv_"))
async def handle_convert_choice(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") != "awaiting_convert_format":
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    target = callback.data[5:]  # убираем "conv_"
    await callback.answer()
    if target == "cancel":
        data["state"] = "awaiting_user_action"
        await callback.message.edit_text(
            "Действие отменено. Выбери другое действие:",
            reply_markup=build_user_video_actions_keyboard()
        )
        return

    # Конвертируем
    await callback.message.edit_text("🔄 Конвертирую видео...")
    input_path = data["video_file"]
    output_path = await convert_video_format(input_path, target)
    if not output_path or not os.path.exists(output_path):
        await callback.message.edit_text("❌ Ошибка конвертации. Попробуй другой формат.")
        data["state"] = "awaiting_user_action"
        await callback.message.edit_text(
            "Выбери действие:",
            reply_markup=build_user_video_actions_keyboard()
        )
        return

    # Заменяем файл
    os.remove(input_path)
    data["video_file"] = output_path
    data["file_info"] = get_video_info(output_path)
    data["state"] = "awaiting_user_action"

    await callback.message.edit_text(
        "✅ Конвертация завершена.\nВыбери дальнейшее действие:",
        reply_markup=build_user_video_actions_keyboard()
    )

@dp.callback_query(F.data == "user_subs")
async def start_user_subs(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") != "awaiting_user_action":
        await callback.answer("Нет видео.", show_alert=True)
        return
    await callback.answer()
    data["state"] = "awaiting_sub_upload"
    await callback.message.edit_text(
        "📤 Отправь мне файл субтитров в формате .srt или .ass.\nИли нажми /skip, чтобы пропустить."
    )

@dp.message(F.document)
async def handle_subtitle_upload(message: Message):
    user_id = message.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") != "awaiting_sub_upload":
        await message.answer("Сначала загрузи видео или ссылку.")
        return

    doc = message.document
    if not doc or not doc.file_name:
        await message.answer("Неверный файл.")
        return
    ext = os.path.splitext(doc.file_name)[1].lower()
    if ext not in (".srt", ".ass"):
        await message.answer("Поддерживаются только .srt и .ass.")
        return

    status = await message.answer("⬇️ Скачиваю субтитры...")
    subs_path = os.path.join(SUBTITLES_DIR, f"user_{user_id}_{doc.file_id}{ext}")
    try:
        await bot.download(doc, destination=subs_path)
    except Exception as e:
        logging.exception("Download subs error")
        await status.edit_text(f"❌ Ошибка: {e}")
        return

    data["subs_file"] = subs_path
    data["state"] = "awaiting_style_user"
    await status.edit_text(
        "🎨 Выбери стиль субтитров:",
        reply_markup=build_style_keyboard()
    )

async def apply_subs_and_send_user_video(callback: CallbackQuery, data: dict):
    """Применяет субтитры к пользовательскому видео и отправляет."""
    user_id = callback.from_user.id
    filepath = data.get("video_file")
    subs_path = data.get("subs_file")
    style_key = data.get("style_key")

    if not filepath or not os.path.exists(filepath):
        await callback.message.edit_text("❌ Видео не найдено.")
        return

    if subs_path and os.path.exists(subs_path) and style_key:
        await callback.message.edit_text("🎞 Накладываю субтитры...")
        new_path = await apply_subtitles(filepath, subs_path, style_key)
        if new_path and os.path.exists(new_path):
            os.remove(filepath)
            filepath = new_path
        else:
            await callback.message.edit_text("⚠️ Не удалось наложить субтитры, отправляю без них.")
        if os.path.exists(subs_path):
            os.remove(subs_path)
    else:
        await callback.message.edit_text("⏩ Субтитры не выбраны, отправляю оригинал.")

    # Отправляем с информацией
    file_info = get_video_info(filepath)
    size_mb = file_info.get("size", 0) / (1024 * 1024)
    duration = file_info.get("duration", 0)
    width = file_info.get("width", 0)
    height = file_info.get("height", 0)
    vcodec = file_info.get("video_codec", "неизвестно")
    acodec = file_info.get("audio_codec", "неизвестно")
    bitrate = file_info.get("bitrate", 0)
    bitrate_str = f"{bitrate//1000} kbps" if bitrate else "неизвестно"

    info_text = (
        f"📄 **{data.get('title', 'video')}**\n"
        f"📦 Размер: {size_mb:.2f} МБ\n"
        f"⏱ Длительность: {format_duration(int(duration))}\n"
        f"🖥 Разрешение: {width}x{height}\n"
        f"🎞 Видео-кодек: {vcodec}\n"
        f"🎵 Аудио-кодек: {acodec}\n"
        f"📊 Битрейт: {bitrate_str}\n"
    )

    if file_info.get("size", 0) > MAX_TELEGRAM_SIZE:
        note = "" if LOCAL_API_URL else "\n\n⚠️ Лимит Telegram 50 МБ."
        await callback.message.edit_text(f"❌ Файл слишком большой ({size_mb:.1f} МБ).{note}")
        os.remove(filepath)
        user_data.pop(user_id, None)
        return

    await callback.message.edit_text("📤 Загружаю в Telegram...")
    await callback.message.answer_video(FSInputFile(filepath), caption=info_text, request_timeout=300, parse_mode="Markdown")
    await callback.message.delete()
    os.remove(filepath)
    user_data.pop(user_id, None)

@dp.callback_query(F.data == "user_send")
async def send_user_video_as_is(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = user_data.get(user_id)
    if not data or data.get("state") != "awaiting_user_action":
        await callback.answer("Нет видео.", show_alert=True)
        return
    await callback.answer()

    filepath = data.get("video_file")
    if not filepath or not os.path.exists(filepath):
        await callback.message.edit_text("❌ Файл не найден.")
        return

    # Отправляем с информацией
    file_info = get_video_info(filepath)
    size_mb = file_info.get("size", 0) / (1024 * 1024)
    duration = file_info.get("duration", 0)
    width = file_info.get("width", 0)
    height = file_info.get("height", 0)
    vcodec = file_info.get("video_codec", "неизвестно")
    acodec = file_info.get("audio_codec", "неизвестно")
    bitrate = file_info.get("bitrate", 0)
    bitrate_str = f"{bitrate//1000} kbps" if bitrate else "неизвестно"

    info_text = (
        f"📄 **{data.get('title', 'video')}**\n"
        f"📦 Размер: {size_mb:.2f} МБ\n"
        f"⏱ Длительность: {format_duration(int(duration))}\n"
        f"🖥 Разрешение: {width}x{height}\n"
        f"🎞 Видео-кодек: {vcodec}\n"
        f"🎵 Аудио-кодек: {acodec}\n"
        f"📊 Битрейт: {bitrate_str}\n"
    )

    if file_info.get("size", 0) > MAX_TELEGRAM_SIZE:
        note = "" if LOCAL_API_URL else "\n\n⚠️ Лимит Telegram 50 МБ."
        await callback.message.edit_text(f"❌ Файл слишком большой ({size_mb:.1f} МБ).{note}")
        os.remove(filepath)
        user_data.pop(user_id, None)
        return

    await callback.message.edit_text("📤 Загружаю в Telegram...")
    await callback.message.answer_video(FSInputFile(filepath), caption=info_text, request_timeout=300, parse_mode="Markdown")
    await callback.message.delete()
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

async def check_pot_provider():
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:4416/ping", timeout=5) as resp:
            if resp.status == 200:
                logging.info("[pot] PO-Token провайдер отвечает на 127.0.0.1:4416")
    except Exception as e:
        logging.warning(f"[pot] PO-Token провайдер недоступен: {e}")

async def main():
    await start_keepalive_server()
    await check_pot_provider()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
