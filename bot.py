"""Instagram, Facebook and Threads media downloader bot."""

import asyncio
import glob
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from telegram import InputMediaPhoto, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

try:
    import streamlink
except ImportError:
    streamlink = None

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
COOKIES_FILE = os.environ.get("COOKIES_FILE", "").strip()
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "5"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "1800"))
DEFAULT_USER_AGENT = os.environ.get(
    "DEFAULT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
)
ALLOWED_USER_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ALLOWED_USER_IDS", ""))}
URL_RE = re.compile(r"^https?://\S+$", re.I)
MANIFEST_RE = re.compile(r"\.(m3u8|m3u|mpd)(?:$|[?#])", re.I)
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".aac", ".flac"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS
DIRECT_EXTENSIONS = MEDIA_EXTENSIONS
_last_request: dict[int, float] = {}


def prepare_cookies_file() -> str | None:
    if not COOKIES_FILE:
        return None
    if os.path.isfile(COOKIES_FILE):
        return COOKIES_FILE
    value = COOKIES_FILE
    if not ("# Netscape HTTP Cookie File" in value or "# HTTP Cookie File" in value or ("instagram.com" in value and "\t" in value)):
        logger.warning("COOKIES_FILE is set but no valid cookies.txt content was detected")
        return None
    path = os.path.join(tempfile.gettempdir(), "bot_cookies.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)
            if not value.endswith("\n"):
                fh.write("\n")
        os.chmod(path, 0o600)
        return path
    except Exception:
        logger.exception("Could not create temporary cookie file")
        return None

COOKIES_PATH = prepare_cookies_file()


def parse_request(text: str) -> tuple[str | None, dict[str, str]]:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines or not URL_RE.match(lines[0]):
        return None, {}
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() and value.strip() and key.strip().lower() not in {"cookie", "authorization"}:
            headers[key.strip()] = value.strip()
    return lines[0], headers


def _is_public_host(host: str) -> bool:
    """Reject hosts that resolve to private/loopback/link-local/reserved addresses."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


async def ensure_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not await asyncio.to_thread(_is_public_host, parsed.hostname or ""):
        raise RuntimeError("This URL is not allowed")


async def _check_request(request: httpx.Request) -> None:
    await ensure_public_url(str(request.url))


def safe_filename(name: str, fallback: str = "media") -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", name).strip(" .")
    return name[:160] or fallback


def filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    return safe_filename(Path(name).stem if name else "media")


def is_image(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def is_video(path: str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def clean_caption(value: str | None, fallback: str = "") -> str:
    if not value:
        return fallback
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value[:1024]


def filename_caption(path: str) -> str:
    name = Path(path).stem
    name = re.sub(r"_ios$", "", name, flags=re.I)
    name = re.sub(r"^[0-9a-f]{32}_", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name).strip(" _.-")
    return name[:1024] or ("Image" if is_image(path) else "Video")


def is_too_large(path: str) -> bool:
    return os.path.getsize(path) > MAX_FILE_SIZE_MB * 1024 * 1024


def newest_media(directory: str) -> list[str]:
    return sorted(
        [p for p in glob.glob(os.path.join(directory, "**", "*"), recursive=True) if os.path.isfile(p) and Path(p).suffix.lower() in MEDIA_EXTENSIONS],
        key=os.path.getmtime,
    )


def run_process(command: list[str], timeout: int = DOWNLOAD_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def download_with_yt_dlp(url: str, headers: dict[str, str], out_dir: str) -> tuple[list[str], str]:
    output = os.path.join(out_dir, f"{uuid.uuid4().hex}_%(title).120s.%(ext)s")
    opts = {
        "outtmpl": output, "format": "bv*[vcodec^=avc]+ba[acodec^=mp4a]/bv*[vcodec^=avc]+ba/bv*+ba/b", "merge_output_format": "mp4",
        "format_sort": ["vcodec:h264", "res", "acodec:aac"],
        "noplaylist": True, "playlist_items": "1-10", "quiet": True, "no_warnings": True, "retries": 3,
        "fragment_retries": 5, "concurrent_fragment_downloads": 4,
        "http_headers": headers, "socket_timeout": 30,
    }
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = newest_media(out_dir)
    caption = clean_caption(
        info.get("description") or info.get("title") if isinstance(info, dict) else None,
        filename_caption(files[0]) if files else "Video",
    )
    return files, caption


def extract_caption_from_json(directory: str) -> str:
    candidates = sorted(glob.glob(os.path.join(directory, "**", "*.json"), recursive=True), key=os.path.getmtime, reverse=True)
    for json_path in candidates:
        try:
            with open(json_path, "r", encoding="utf-8", errors="ignore") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for key in ("description", "content", "caption", "title"):
                    if data.get(key):
                        return clean_caption(data[key])
                for nested_key in ("post", "media", "metadata"):
                    nested = data.get(nested_key)
                    if isinstance(nested, dict):
                        for key in ("description", "content", "caption", "title"):
                            if nested.get(key):
                                return clean_caption(nested[key])
        except Exception:
            continue
    return ""


def download_with_gallery_dl(url: str, out_dir: str) -> tuple[list[str], str]:
    binary = shutil.which("gallery-dl") or shutil.which("gallery-dl.exe")
    if not binary:
        raise RuntimeError("gallery-dl is not installed")
    command = [binary, "--directory", out_dir, "--no-mtime", "--write-info-json"]
    if COOKIES_PATH:
        command += ["--cookies", COOKIES_PATH]
    command += [url]
    result = run_process(command)
    files = newest_media(out_dir)
    if result.returncode != 0 and not files:
        raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:] or "gallery-dl failed")
    if not files:
        raise RuntimeError("gallery-dl completed but produced no media")
    return files, (extract_caption_from_json(out_dir) or filename_caption(files[0]))


def extract_cookie_header(cookie_path: str, target_url: str) -> str:
    host = (urlparse(target_url).hostname or "").lower()
    pairs = []
    try:
        with open(cookie_path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 7:
                    domain, _, _, _, _, name, value = parts[:7]
                    domain = domain.lstrip(".").lower()
                    if host == domain or host.endswith("." + domain):
                        pairs.append(f"{name}={value}")
    except OSError:
        pass
    return "; ".join(pairs)


def download_with_n_m3u8dl(url: str, headers: dict[str, str], out_dir: str) -> tuple[list[str], str]:
    binary = shutil.which("N_m3u8DL-RE") or shutil.which("N_m3u8DL-RE.exe")
    if not binary:
        raise RuntimeError("N_m3u8DL-RE is not installed")
    command = [binary, url, "--save-dir", out_dir, "--save-name", safe_filename(filename_from_url(url)), "--del-after-done", "--no-date-info", "--no-log", "--concurrent-download", "--download-retry-count", "5", "--http-request-timeout", "60", "--ffmpeg-binary-path", shutil.which("ffmpeg") or "/usr/bin/ffmpeg"]
    if COOKIES_PATH:
        cookie_header = extract_cookie_header(COOKIES_PATH, url)
        if cookie_header:
            command += ["-H", f"Cookie: {cookie_header}"]
    for key, value in headers.items():
        command += ["-H", f"{key}: {value}"]
    result = run_process(command)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2500:] or result.stdout[-2500:] or "N_m3u8DL-RE failed")
    files = newest_media(out_dir)
    return files, (filename_caption(files[0]) if files else "Video")


def download_with_streamlink(url: str, headers: dict[str, str], out_dir: str) -> tuple[list[str], str]:
    if not streamlink:
        raise RuntimeError("Streamlink is not installed")
    raw_path = os.path.join(out_dir, "stream.ts")
    command = ["streamlink", "--loglevel", "error"]
    for key, value in headers.items():
        command += ["--http-header", f"{key}={value}"]
    if COOKIES_PATH:
        command += ["--http-cookies-file", COOKIES_PATH]
    command += [url, "best", "-o", raw_path]
    result = run_process(command)
    if result.returncode != 0 or not os.path.exists(raw_path):
        raise RuntimeError(result.stderr[-2500:] or result.stdout[-2500:] or "Streamlink failed")
    mp4_path = os.path.join(out_dir, "stream.mp4")
    result = run_process([shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", raw_path, "-c", "copy", mp4_path])
    if result.returncode != 0 or not os.path.exists(mp4_path):
        raise RuntimeError(result.stderr[-2500:] or "FFmpeg remux failed")
    return [mp4_path], "Video"


async def download_direct(url: str, headers: dict[str, str], out_dir: str) -> tuple[list[str], str]:
    filename = filename_from_url(url)
    if not Path(filename).suffix:
        filename += ".mp4"
    path = os.path.join(out_dir, safe_filename(filename))
    async with httpx.AsyncClient(follow_redirects=True, max_redirects=5, timeout=120, headers=headers, event_hooks={"request": [_check_request]}) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type and not Path(urlparse(str(response.url)).path).suffix:
                raise RuntimeError("URL returned a webpage instead of media")
            ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
            if ext in DIRECT_EXTENSIONS:
                path = os.path.join(out_dir, safe_filename(Path(filename).stem + ext))
            with open(path, "wb") as fh:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    fh.write(chunk)
    return [path], filename_caption(path)


async def process_url(url: str, headers: dict[str, str], work_dir: str) -> tuple[list[str], str]:
    await ensure_public_url(url)
    manifest = bool(MANIFEST_RE.search(url.lower()))
    suffix = Path(urlparse(url).path).suffix.lower()
    host = (urlparse(url).hostname or "").lower()
    is_social = host.endswith(("instagram.com", "facebook.com", "fb.watch", "threads.com", "threads.net"))

    if suffix in DIRECT_EXTENSIONS:
        try:
            files, caption = await download_direct(url, headers, work_dir)
            if files and not is_too_large(files[0]):
                return files, caption
        except Exception as exc:
            logger.info("Direct download failed: %s", exc)

    if manifest:
        try:
            return await asyncio.to_thread(download_with_n_m3u8dl, url, headers, work_dir)
        except Exception as exc:
            logger.warning("N_m3u8DL-RE failed: %s", exc)
        try:
            return await asyncio.to_thread(download_with_yt_dlp, url, headers, work_dir)
        except Exception as exc:
            logger.warning("yt-dlp manifest fallback failed: %s", exc)
        return await asyncio.to_thread(download_with_streamlink, url, headers, work_dir)

    try:
        files, caption = await asyncio.to_thread(download_with_yt_dlp, url, headers, work_dir)
        if files:
            return files, caption
    except Exception as exc:
        logger.warning("yt-dlp failed: %s", exc)

    if is_social:
        try:
            return await asyncio.to_thread(download_with_gallery_dl, url, work_dir)
        except Exception as exc:
            logger.warning("gallery-dl image fallback failed: %s", exc)

    try:
        return await asyncio.to_thread(download_with_streamlink, url, headers, work_dir)
    except Exception as exc:
        logger.warning("Streamlink failed: %s", exc)
        raise RuntimeError("All available download engines failed") from exc


def probe_video(path: str) -> dict:
    """Return codec/size info for the first video stream (empty dict if none)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}
    try:
        r = run_process([ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path], timeout=60)
        data = json.loads(r.stdout or "{}")
    except Exception:
        return {}
    video = next((x for x in data.get("streams", []) if x.get("codec_type") == "video"), None)
    audio = next((x for x in data.get("streams", []) if x.get("codec_type") == "audio"), None)
    if not video:
        return {}
    try:
        duration = int(float(data.get("format", {}).get("duration", 0)))
    except (TypeError, ValueError):
        duration = 0
    return {
        "vcodec": video.get("codec_name"), "pix_fmt": video.get("pix_fmt"),
        "width": video.get("width"), "height": video.get("height"),
        "acodec": audio.get("codec_name") if audio else None, "duration": duration,
        "container": data.get("format", {}).get("format_name", ""),
    }


def ensure_telegram_compatible(path: str) -> str:
    """Make sure the video is H.264 + yuv420p + AAC in an MP4 so Telegram plays the picture, not only audio."""
    if not is_video(path):
        return path
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to prepare videos for Telegram")
    info = probe_video(path)
    already_ok = (
        info.get("vcodec") == "h264" and info.get("pix_fmt") == "yuv420p"
        and info.get("acodec") in (None, "aac") and "mp4" in info.get("container", "")
        and Path(path).suffix.lower() in {".mp4", ".m4v", ".mov"}
        and (info.get("width") or 0) <= 1920 and (info.get("height") or 0) <= 1920
        and not is_too_large(path)
    )
    output = os.path.join(os.path.dirname(path), f"{Path(path).stem}_tg.mp4")
    if already_ok:
        command = [ffmpeg, "-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", output]
    else:
        vf = "scale=w='min(1920,iw)':h='min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p"
        command = [ffmpeg, "-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-profile:v", "high", "-level:v", "4.2", "-pix_fmt", "yuv420p", "-tag:v", "avc1", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2", "-map_metadata", "-1", "-movflags", "+faststart", output]
    result = run_process(command, timeout=DOWNLOAD_TIMEOUT)
    if result.returncode != 0 or not os.path.isfile(output) or os.path.getsize(output) == 0:
        logger.error("Telegram MP4 conversion failed: %s", result.stderr[-3000:])
        if already_ok:
            return path
        raise RuntimeError("Could not convert this video to a Telegram-compatible format")
    if path != output:
        try:
            os.remove(path)
        except OSError:
            pass
    return output


async def send_single(update: Update, path: str, caption: str) -> None:
    if is_video(path):
        path = await asyncio.to_thread(ensure_telegram_compatible, path)
        if is_too_large(path):
            raise RuntimeError(f"Converted video is too large for Telegram's current bot upload limit ({MAX_FILE_SIZE_MB} MB).")
        with open(path, "rb") as media:
            try:
                meta = await asyncio.to_thread(probe_video, path)
                await update.message.reply_video(video=media, caption=caption, supports_streaming=True, width=meta.get("width"), height=meta.get("height"), duration=meta.get("duration"))
            except Exception as exc:
                logger.warning("Telegram video upload failed, falling back to document: %s", exc)
                media.seek(0)
                await update.message.reply_document(document=media, caption=caption)
        return
    if is_image(path):
        with open(path, "rb") as media:
            try:
                await update.message.reply_photo(photo=media, caption=caption)
            except Exception as exc:
                logger.warning("Telegram photo upload failed, falling back to document: %s", exc)
                media.seek(0)
                await update.message.reply_document(document=media, caption=caption)
        return
    with open(path, "rb") as media:
        await update.message.reply_document(document=media, caption=caption)


async def send_image_album(update: Update, files: list[str], caption: str) -> None:
    files = files[:20]
    for start in range(0, len(files), 10):
        chunk = files[start:start + 10]
        handles = []
        media_group = []
        try:
            for index, path in enumerate(chunk):
                fh = open(path, "rb")
                handles.append(fh)
                media_group.append(InputMediaPhoto(media=fh, caption=caption if start == 0 and index == 0 else None))
            await update.message.reply_media_group(media=media_group)
        finally:
            for fh in handles:
                fh.close()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 **Hey! Welcome!**\n\n"
        "📥 **Instagram, Facebook & Threads Downloader**\n\n"
        "🔗 Bas Reel, Video, Story, Post ya Threads link yahan paste karo.\n"
        "⚡️ Main media process karke directly yahin send kar dunga.\n\n"
        "**No extra steps. Just paste the link! 🚀**",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return
    now = time.time()
    wait_left = COOLDOWN_SECONDS - (now - _last_request.get(user_id, 0))
    if wait_left > 0:
        await update.message.reply_text(f"⏳ Please wait {int(wait_left) + 1}s before sending another link.")
        return
    url, headers = parse_request(update.message.text)
    if not url:
        await update.message.reply_text("🔗 **That doesn't look like a valid URL.**\n\nPlease send a video, image, stream, M3U8/HLS, or direct media link.", parse_mode="Markdown")
        return
    _last_request[user_id] = now
    status = await update.message.reply_text("⏳ **Processing your media...**", parse_mode="Markdown")
    with tempfile.TemporaryDirectory(prefix="tgdl_") as work_dir:
        try:
            files, caption = await process_url(url, headers, work_dir)
            files = [f for f in files if os.path.isfile(f) and (not is_too_large(f) or is_video(f))]
            if not files:
                raise RuntimeError("No media file was produced")
            caption = clean_caption(caption, filename_caption(files[0]))
            await status.edit_text("📤 **Almost there!**\n\nYour media is ready — sending it now... ✨", parse_mode="Markdown")

            image_files = [f for f in files if is_image(f)]
            if len(image_files) > 1 and len(image_files) == len(files):
                await send_image_album(update, image_files, caption)
            else:
                for path in files[:20]:
                    await send_single(update, path, caption)
            await status.delete()
        except Exception as exc:
            logger.exception("Download failed for %s", url)
            await status.edit_text(
                "❌ **Couldn't download this media**\n\n"
                "The link may be private, expired, login-required, protected, or unsupported."
            )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set")
    logger.info("Starting bot | cookies=%s | gallery-dl=%s | N_m3u8DL-RE=%s | ffmpeg=%s | streamlink=%s", "YES" if COOKIES_PATH else "NO", "YES" if shutil.which("gallery-dl") else "NO", "YES" if shutil.which("N_m3u8DL-RE") else "NO", "YES" if shutil.which("ffmpeg") else "NO", "YES" if streamlink else "NO")
    app = Application.builder().token(BOT_TOKEN).connect_timeout(30).read_timeout(120).write_timeout(120).pool_timeout(30).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

