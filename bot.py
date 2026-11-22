"""Instagram, Facebook and Threads media downloader bot (Telegram)."""

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
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

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
# httpx logs full request URLs at INFO level, and Telegram's API URL contains the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class UserError(RuntimeError):
    """An error whose message is safe to show to the Telegram user."""


BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
COOKIES_FILE = os.environ.get("COOKIES_FILE", "").strip()
# Comma/space separated Telegram numeric user IDs allowed to use the bot.
ALLOWED_USERS = {
    int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USERS", "")) if x.strip().isdigit()
}
# Telegram's standard Bot API caps uploads at 50 MB.
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "5"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "1800"))
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "2")))
MAX_PLAYLIST_ITEMS = int(os.environ.get("MAX_PLAYLIST_ITEMS", "10"))
MAX_REDIRECTS = 5
DEFAULT_USER_AGENT = os.environ.get(
    "DEFAULT_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
)
URL_RE = re.compile(r"^https?://\S+$", re.I)
MANIFEST_RE = re.compile(r"\.(m3u8|m3u|mpd)(?:$|[?#])", re.I)
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".aac", ".flac"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS
DIRECT_EXTENSIONS = MEDIA_EXTENSIONS
SOCIAL_HOSTS = ("instagram.com", "facebook.com", "fb.watch", "threads.com", "threads.net")

_last_request: dict[int, float] = {}
_job_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)


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
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
            if not value.endswith("\n"):
                fh.write("\n")
        return path
    except Exception:
        logger.exception("Could not create temporary cookie file")
        return None


COOKIES_PATH = prepare_cookies_file()


def is_allowed(user_id: int) -> bool:
    """If ALLOWED_USERS is empty the bot is open to everyone; otherwise only listed users may use it."""
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def is_safe_url(url: str) -> bool:
    """Reject non-http(s) URLs and hosts resolving to private/loopback/link-local/reserved IPs (SSRF guard)."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (ValueError, UnicodeError, socket.gaierror):
        return False
    if not infos:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if not ip.is_global:
            return False
    return True


def parse_request(text: str) -> tuple[str | None, dict[str, str]]:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines or not URL_RE.match(lines[0]):
        return None, {}
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() and value.strip() and key.strip().lower() not in {"cookie", "authorization", "host"}:
            headers[key.strip()] = value.strip()
    return lines[0], headers


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
    name = re.sub(r"_(ios|tg)$", "", name, flags=re.I)
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
        "outtmpl": output,
        # Prefer H.264 + AAC (plays on every Telegram client and needs no re-encode), capped at 1080p.
        # VP9/AV1/HEVC are only used when nothing else exists; those get transcoded afterwards.
        "format": "bv*+ba/b",
        "format_sort": ["vcodec:h264", "res:1080", "acodec:aac", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        # Instagram carousels are playlists in yt-dlp, so cap the item count instead of disabling playlists.
        "noplaylist": False, "playlistend": MAX_PLAYLIST_ITEMS,
        "max_filesize": MAX_FILE_SIZE_MB * 1024 * 1024,
        "quiet": True, "no_warnings": True, "noprogress": True,
        "retries": 2, "extractor_retries": 1, "fragment_retries": 3,
        "concurrent_fragment_downloads": 8,
        "http_headers": headers, "socket_timeout": 20,
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


def extract_cookie_pairs(cookie_path: str, target_url: str) -> list[str]:
    """Return 'name=value' cookies from a Netscape cookies.txt that apply to target_url's host."""
    host = (urlparse(target_url).hostname or "").lower()
    pairs = []
    try:
        with open(cookie_path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.rstrip("\r\n")
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]  # HttpOnly cookies (e.g. Instagram sessionid) carry this prefix
                elif not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    domain, _, _, _, _, name, value = parts[:7]
                    domain = domain.lstrip(".").lower()
                    if host == domain or host.endswith("." + domain):
                        pairs.append(f"{name}={value}")
    except OSError:
        pass
    return pairs


def download_with_n_m3u8dl(url: str, headers: dict[str, str], out_dir: str) -> tuple[list[str], str]:
    binary = shutil.which("N_m3u8DL-RE") or shutil.which("N_m3u8DL-RE.exe")
    if not binary:
        raise RuntimeError("N_m3u8DL-RE is not installed")
    command = [binary, url, "--save-dir", out_dir, "--save-name", safe_filename(filename_from_url(url)), "--del-after-done", "--no-date-info", "--no-log", "--concurrent-download", "--download-retry-count", "5", "--http-request-timeout", "60", "--ffmpeg-binary-path", shutil.which("ffmpeg") or "/usr/bin/ffmpeg"]
    if COOKIES_PATH:
        pairs = extract_cookie_pairs(COOKIES_PATH, url)
        if pairs:
            command += ["-H", "Cookie: " + "; ".join(pairs)]
    for key, value in headers.items():
        command += ["-H", f"{key}: {value}"]
    result = run_process(command)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2500:] or result.stdout[-2500:] or "N_m3u8DL-RE failed")
    files = newest_media(out_dir)
    return files, (filename_caption(files[0]) if files else "Video")


def download_with_streamlink(url: str, headers: dict[str, str], out_dir: str) -> tuple[list[str], str]:
    binary = shutil.which("streamlink")
    if not streamlink or not binary:
        raise RuntimeError("Streamlink is not installed")
    raw_path = os.path.join(out_dir, "stream.ts")
    command = [binary, "--loglevel", "error"]
    for key, value in headers.items():
        command += ["--http-header", f"{key}={value}"]
    if COOKIES_PATH:
        # Cookies file stays domain-scoped (unlike --http-cookie, which sends cookies on every request).
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
    limit = MAX_FILE_SIZE_MB * 1024 * 1024
    url_suffix = Path(urlparse(url).path).suffix.lower()
    filename = filename_from_url(url) + (url_suffix if url_suffix in DIRECT_EXTENSIONS else ".mp4")
    path = os.path.join(out_dir, safe_filename(filename))
    timeout = httpx.Timeout(60.0, read=120.0)
    # Redirects are followed manually so every hop is re-validated against the SSRF guard.
    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, headers=headers) as client:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if not await asyncio.to_thread(is_safe_url, current):
                raise UserError("This URL is not allowed.")
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise RuntimeError("Redirect without a Location header")
                    current = urljoin(str(response.url), location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if "text/html" in content_type and not Path(urlparse(str(response.url)).path).suffix:
                    raise RuntimeError("URL returned a webpage instead of media")
                try:
                    declared = int(response.headers.get("content-length") or 0)
                except ValueError:
                    declared = 0
                if declared > limit:
                    raise UserError(f"File is larger than the {MAX_FILE_SIZE_MB} MB upload limit.")
                ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
                if ext in DIRECT_EXTENSIONS:
                    path = os.path.join(out_dir, safe_filename(Path(filename).stem + ext))
                written = 0
                with open(path, "wb") as fh:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        written += len(chunk)
                        if written > limit:
                            raise UserError(f"File is larger than the {MAX_FILE_SIZE_MB} MB upload limit.")
                        fh.write(chunk)
                return [path], filename_caption(path)
    raise RuntimeError("Too many redirects")


async def process_url(url: str, headers: dict[str, str], work_dir: str) -> tuple[list[str], str]:
    manifest = bool(MANIFEST_RE.search(url.lower()))
    suffix = Path(urlparse(url).path).suffix.lower()
    host = (urlparse(url).hostname or "").lower()
    is_social = host.endswith(SOCIAL_HOSTS)

    if suffix in DIRECT_EXTENSIONS:
        try:
            files, caption = await download_direct(url, headers, work_dir)
            if files and not is_too_large(files[0]):
                return files, caption
        except UserError:
            raise
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
        try:
            return await asyncio.to_thread(download_with_streamlink, url, headers, work_dir)
        except Exception as exc:
            logger.warning("Streamlink failed: %s", exc)
            raise UserError("All available download engines failed.") from exc

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
        # Streamlink has no plugin for these sites, so don't spend more time on it.
        raise UserError("Instagram/Facebook returned no media. The post may be private or deleted, or your cookies may have expired.")

    try:
        return await asyncio.to_thread(download_with_streamlink, url, headers, work_dir)
    except Exception as exc:
        logger.warning("Streamlink failed: %s", exc)
        raise UserError("All available download engines failed.") from exc


H264_OK_PROFILES = {"Baseline", "Constrained Baseline", "Main", "High"}
AAC_ARGS = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]


def probe_video(path: str) -> dict | None:
    """Playback facts about a video file, or None if ffprobe is unavailable or fails."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = run_process(
            [ffprobe, "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,profile,level,pix_fmt,width,height:format=duration",
             "-of", "json", path],
            timeout=60,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None
    streams = data.get("streams", [])
    video = [x for x in streams if x.get("codec_type") == "video"]
    audio = [x for x in streams if x.get("codec_type") == "audio"]
    if not video:
        return None
    v = video[0]
    width, height = int(v.get("width") or 0), int(v.get("height") or 0)
    level = v.get("level")
    video_ok = (
        v.get("codec_name") == "h264"
        and v.get("pix_fmt") == "yuv420p"
        and v.get("profile") in H264_OK_PROFILES
        and (not isinstance(level, int) or level <= 42)
        and 0 < max(width, height) <= 1920
        and 0 < min(width, height) <= 1080
    )
    audio_ok = all(x.get("codec_name") == "aac" for x in audio)
    try:
        duration = int(float(data.get("format", {}).get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0
    return {"video_ok": video_ok, "audio_ok": audio_ok, "width": width, "height": height, "duration": duration}


def _telegram_meta(info: dict | None) -> dict:
    if info and info.get("width") and info.get("height"):
        return {"width": info["width"], "height": info["height"], "duration": info.get("duration", 0)}
    return {}


def ensure_telegram_compatible(path: str) -> tuple[str, dict]:
    """Make a video play everywhere: H.264 + AAC in an MP4 with faststart.

    Handles ANY container (webm/mkv/mov/...), not just .mp4. Players that can't decode the video codec
    (VP9/AV1/HEVC/10-bit/4K) show audio only, so those streams must be re-encoded.
    Returns (path, metadata for Telegram).
    """
    if not is_video(path):
        return path, {}
    info = probe_video(path)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return path, _telegram_meta(info)

    output = os.path.join(os.path.dirname(path), f"{Path(path).stem}_tg.mp4")
    scale = "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p"
    full = ["-vf", scale, "-c:v", "libx264", "-preset", "superfast", "-crf", "24", "-profile:v", "high",
            "-level:v", "4.2", "-pix_fmt", "yuv420p", "-tag:v", "avc1",
            *(["-c:a", "copy"] if info and info["audio_ok"] else AAC_ARGS)]
    if info and info["video_ok"] and info["audio_ok"]:
        attempts = [("remux", ["-c", "copy"]), ("transcode", full)]          # already compatible: just fix container
    elif info and info["video_ok"]:
        attempts = [("audio-transcode", ["-c:v", "copy", *AAC_ARGS]), ("transcode", full)]  # copy video, fix audio only
    else:
        attempts = [("transcode", full)]                                      # video codec/size not playable everywhere

    for mode, codec_args in attempts:
        started = time.perf_counter()
        command = [ffmpeg, "-y", "-i", path, "-map", "0:v:0", "-map", "0:a:0?", *codec_args,
                   "-map_metadata", "-1", "-movflags", "+faststart", output]
        result = run_process(command, timeout=DOWNLOAD_TIMEOUT)
        if result.returncode == 0 and os.path.isfile(output) and os.path.getsize(output) > 0:
            logger.info("ffmpeg %s took %.1fs for %s", mode, time.perf_counter() - started, Path(path).name)
            try:
                os.remove(path)
            except OSError:
                pass
            return output, _telegram_meta(probe_video(output) or info)
        logger.error("ffmpeg %s failed: %s", mode, result.stderr[-1500:])
    return path, _telegram_meta(info)


async def send_single(update: Update, path: str, caption: str) -> None:
    if is_video(path):
        path, meta = await asyncio.to_thread(ensure_telegram_compatible, path)
        if is_too_large(path):
            raise UserError(f"Video is larger than Telegram's bot upload limit ({MAX_FILE_SIZE_MB} MB).")
        with open(path, "rb") as media:
            try:
                await update.message.reply_video(video=media, caption=caption, supports_streaming=True, **meta)
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
    user_id = update.effective_user.id if update.effective_user else 0
    if not update.message or not is_allowed(user_id):
        return
    await update.message.reply_text(
        "👋 **Hey! Welcome!**\n\n"
        "📥 **Instagram, Facebook & Threads Downloader**\n\n"
        "🔗 Bas Reel, Video, Story, Post ya Threads link yahan paste karo.\n"
        "⚡️ Main media process karke directly yahin send kar dunga.\n\n"
        "**No extra steps. Just paste the link! 🚀**\n\n"
        "👤 Instagram **profile link** doge to main posts ginke bata dunga (reels/videos kitni, images kitni) "
        "aur tum chuno ki kya aur kitna download karna hai.",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    if not is_allowed(user_id):
        logger.warning("Ignored message from unauthorized user %s", user_id)
        return
    now = time.time()
    if len(_last_request) > 1000:
        for key in [k for k, v in _last_request.items() if v < now - 3600]:
            del _last_request[key]
    wait_left = COOLDOWN_SECONDS - (now - _last_request.get(user_id, 0))
    if wait_left > 0:
        await update.message.reply_text(f"⏳ Please wait {int(wait_left) + 1}s before sending another link.")
        return
    url, headers = parse_request(update.message.text)
    if not url:
        await update.message.reply_text("🔗 **That doesn't look like a valid URL.**\n\nPlease send a video, image, stream, M3U8/HLS, or direct media link.", parse_mode="Markdown")
        return
    _last_request[user_id] = now  # set before any await so concurrent updates can't bypass the cooldown
    if not await asyncio.to_thread(is_safe_url, url):
        await update.message.reply_text("❌ This URL is not allowed.")
        return
    status = await update.message.reply_text("⏳ **Processing your media...**", parse_mode="Markdown")
    async with _job_slots:
        with tempfile.TemporaryDirectory(prefix="tgdl_") as work_dir:
            try:
                t_fetch = time.perf_counter()
                files, caption = await process_url(url, headers, work_dir)
                t_fetch = time.perf_counter() - t_fetch
                t_send = time.perf_counter()
                existing = [f for f in files if os.path.isfile(f)]
                files = [f for f in existing if not is_too_large(f)]
                if not files:
                    if existing:
                        raise UserError(f"The file is larger than the {MAX_FILE_SIZE_MB} MB Telegram upload limit.")
                    raise UserError("No media file was produced.")
                caption = clean_caption(caption, filename_caption(files[0]))
                await status.edit_text("📤 **Almost there!**\n\nYour media is ready — sending it now... ✨", parse_mode="Markdown")

                image_files = [f for f in files if is_image(f)]
                if len(image_files) > 1 and len(image_files) == len(files):
                    await send_image_album(update, image_files, caption)
                else:
                    for path in files[:20]:
                        await send_single(update, path, caption)
                await status.delete()
                logger.info("Done %s | fetch=%.1fs prepare+send=%.1fs", urlparse(url).hostname, t_fetch, time.perf_counter() - t_send)
            except Exception as exc:
                logger.exception("Download failed for %s", url)
                reason = str(exc)[:300] if isinstance(exc, UserError) else "see the server logs for details"
                await status.edit_text(
                    "❌ Couldn't download this media\n\n"
                    "The link may be private, expired, login-required, protected, or unsupported.\n\n"
                    f"Reason: {reason}"
                )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing an update", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set")
    if not ALLOWED_USERS:
        logger.warning("ALLOWED_USERS is not set: anyone who finds the bot can use it (and your server's bandwidth/cookies).")
    logger.info(
        "Starting bot | users=%s | cookies=%s | gallery-dl=%s | N_m3u8DL-RE=%s | ffmpeg=%s | streamlink=%s",
        len(ALLOWED_USERS) or "everyone", "YES" if COOKIES_PATH else "NO", "YES" if shutil.which("gallery-dl") else "NO",
        "YES" if shutil.which("N_m3u8DL-RE") else "NO", "YES" if shutil.which("ffmpeg") else "NO", "YES" if streamlink else "NO",
    )
    app = (
        Application.builder().token(BOT_TOKEN)
        .concurrent_updates(True)
        .connect_timeout(30).read_timeout(120).write_timeout(120).pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(on_error)
    import profile_feature  # Instagram profile scan + bulk download
    profile_feature.register(app, sys.modules[__name__])
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
