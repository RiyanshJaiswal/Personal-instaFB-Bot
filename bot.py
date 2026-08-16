"""Universal Telegram media downloader.

Send a normal website URL, direct media URL, M3U8/HLS URL, or MPD URL.
The bot tries the most appropriate downloader and sends the result back to Telegram.

Optional Telegram message format for future desktop/IDM helper:
    https://example.com/video.m3u8
    Referer: https://example.com/page
    User-Agent: Mozilla/5.0

Cookies are supplied through COOKIES_FILE as either a path or Netscape cookies.txt
content. Never commit cookies or bot tokens to GitHub.
"""

import glob
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

try:
    import streamlink
except ImportError:  # pragma: no cover
    streamlink = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
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

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
MANIFEST_RE = re.compile(r"\.(m3u8|m3u|mpd)(?:$|[?#])", re.IGNORECASE)
DIRECT_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ts", ".m4a", ".mp3", ".aac", ".flac"
}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ts"}

_last_request: dict[int, float] = {}


def prepare_cookies_file() -> str | None:
    """Resolve COOKIES_FILE as a real path or inline Netscape cookie content."""
    if not COOKIES_FILE:
        return None

    if os.path.isfile(COOKIES_FILE):
        return COOKIES_FILE

    value = COOKIES_FILE
    looks_like_netscape = (
        "# Netscape HTTP Cookie File" in value
        or "# HTTP Cookie File" in value
        or ("instagram.com" in value and "\t" in value)
    )
    if not looks_like_netscape:
        logger.warning("COOKIES_FILE is set but no valid cookies.txt content was detected")
        return None

    path = os.path.join(tempfile.gettempdir(), "bot_cookies.txt")
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(value)
            if not value.endswith("\n"):
                fh.write("\n")
        os.chmod(path, 0o600)
        logger.info("Cookies loaded: YES")
        return path
    except Exception:
        logger.exception("Could not create temporary cookies file")
        return None


COOKIES_PATH = prepare_cookies_file()


def parse_request(text: str) -> tuple[str | None, dict[str, str]]:
    """Parse a URL and optional HTTP headers from a Telegram message."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None, {}

    url = lines[0]
    if not URL_RE.match(url):
        return None, {}

    headers: dict[str, str] = {"User-Agent": DEFAULT_USER_AGENT}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and value and key.lower() not in {"cookie", "authorization"}:
            headers[key] = value
    return url, headers


def safe_filename(name: str, fallback: str = "video") -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", name).strip(" .")
    return (name[:160] or fallback)


def filename_from_url(url: str) -> str:
    path_name = Path(urlparse(url).path).name
    stem = Path(path_name).stem if path_name else "video"
    return safe_filename(stem or "video")


def is_too_large(path: str) -> bool:
    return os.path.getsize(path) > MAX_FILE_SIZE_MB * 1024 * 1024


def newest_media(directory: str) -> list[str]:
    files = []
    for path in glob.glob(os.path.join(directory, "**", "*"), recursive=True):
        if os.path.isfile(path) and Path(path).suffix.lower() in VIDEO_EXTENSIONS:
            files.append(path)
    return sorted(files, key=os.path.getmtime)


def run_process(command: list[str], timeout: int = DOWNLOAD_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def download_with_yt_dlp(url: str, headers: dict[str, str], out_dir: str) -> list[str]:
    output = os.path.join(out_dir, f"{uuid.uuid4().hex}_%(title).120s.%(ext)s")
    opts = {
        "outtmpl": output,
        "format": "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "http_headers": headers,
        "socket_timeout": 30,
    }
    if COOKIES_PATH:
        opts["cookiefile"] = COOKIES_PATH

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    return newest_media(out_dir)


def download_with_n_m3u8dl(url: str, headers: dict[str, str], out_dir: str) -> list[str]:
    binary = shutil.which("N_m3u8DL-RE") or shutil.which("N_m3u8DL-RE.exe")
    if not binary:
        raise RuntimeError("N_m3u8DL-RE is not installed")

    name = safe_filename(filename_from_url(url))
    command = [
        binary,
        url,
        "--save-dir", out_dir,
        "--save-name", name,
        "--del-after-done",
        "--no-date-info",
        "--no-log",
        "--concurrent-download",
        "--download-retry-count", "5",
        "--http-request-timeout", "60",
        "--ffmpeg-binary-path", shutil.which("ffmpeg") or "/usr/bin/ffmpeg",
    ]
    if COOKIES_PATH:
        # N_m3u8DL-RE accepts custom headers; cookies are passed as a Cookie header.
        cookie_header = extract_cookie_header(COOKIES_PATH, url)
        if cookie_header:
            command += ["-H", f"Cookie: {cookie_header}"]
    for key, value in headers.items():
        command += ["-H", f"{key}: {value}"]

    result = run_process(command)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-2500:] or result.stdout[-2500:] or "N_m3u8DL-RE failed")
    return newest_media(out_dir)


def extract_cookie_header(cookie_path: str, target_url: str) -> str:
    """Build a simple Cookie header from Netscape cookies matching the target host."""
    host = (urlparse(target_url).hostname or "").lower()
    pairs: list[str] = []
    try:
        with open(cookie_path, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 7:
                    continue
                domain, include_subdomains, _, _, _, name, value = parts[:7]
                domain = domain.lstrip(".").lower()
                if host == domain or host.endswith("." + domain):
                    pairs.append(f"{name}={value}")
    except OSError:
        return ""
    return "; ".join(pairs)


def download_with_streamlink(url: str, headers: dict[str, str], out_dir: str) -> list[str]:
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
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    result = run_process([ffmpeg, "-y", "-i", raw_path, "-c", "copy", mp4_path])
    if result.returncode != 0 or not os.path.exists(mp4_path):
        raise RuntimeError(result.stderr[-2500:] or "FFmpeg remux failed")
    return [mp4_path]


async def download_direct(url: str, headers: dict[str, str], out_dir: str) -> list[str]:
    parsed = urlparse(url)
    filename = filename_from_url(url)
    if not Path(filename).suffix:
        filename += ".mp4"
    path = os.path.join(out_dir, safe_filename(filename))

    async with httpx.AsyncClient(follow_redirects=True, timeout=None, headers=headers) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type and not Path(urlparse(str(response.url)).path).suffix:
                raise RuntimeError("URL returned a webpage instead of a media file")
            if not Path(path).suffix or Path(path).suffix == ".mp4":
                ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
                if ext in DIRECT_EXTENSIONS:
                    path = os.path.join(out_dir, safe_filename(Path(filename).stem + ext))
            with open(path, "wb") as fh:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    fh.write(chunk)
    return [path]


async def process_url(url: str, headers: dict[str, str], work_dir: str) -> list[str]:
    lower = url.lower()
    manifest = bool(MANIFEST_RE.search(lower))

    # Direct media files should not be routed through a site extractor.
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in DIRECT_EXTENSIONS:
        try:
            files = await download_direct(url, headers, work_dir)
            if files and not is_too_large(files[0]):
                return files
        except Exception as exc:
            logger.info("Direct download failed: %s", exc)

    if manifest:
        try:
            return download_with_n_m3u8dl(url, headers, work_dir)
        except Exception as exc:
            logger.warning("N_m3u8DL-RE failed: %s", exc)
        try:
            return download_with_yt_dlp(url, headers, work_dir)
        except Exception as exc:
            logger.warning("yt-dlp manifest fallback failed: %s", exc)
        return download_with_streamlink(url, headers, work_dir)

    try:
        return download_with_yt_dlp(url, headers, work_dir)
    except Exception as exc:
        logger.warning("yt-dlp failed: %s", exc)

    # A site may expose a stream even when the page URL is not obviously a manifest.
    try:
        return download_with_streamlink(url, headers, work_dir)
    except Exception as exc:
        logger.warning("Streamlink failed: %s", exc)
        raise RuntimeError("All available download engines failed") from exc


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 **Hey! Welcome!**\n\n"
        "📥 **Universal Media Downloader**\n\n"
        "🔗 Bas video, Reel, Story, stream ya direct media link yahan paste karo.\n"
        "⚡ Main usse process karke media directly yahin bhej dunga.\n\n"
        "**No extra steps. Just paste the link! 🚀**",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    now = time.time()
    wait_left = COOLDOWN_SECONDS - (now - _last_request.get(user_id, 0))
    if wait_left > 0:
        await update.message.reply_text(f"⏳ Please wait {int(wait_left) + 1}s before sending another link.")
        return

    url, headers = parse_request(update.message.text)
    if not url:
        await update.message.reply_text(
            "🔗 **That doesn't look like a valid URL.**\n\n"
            "Please send an Instagram, Facebook, video, stream, M3U8/HLS, or direct media link.",
            parse_mode="Markdown",
        )
        return

    _last_request[user_id] = now
    status = await update.message.reply_text("⏳ **Processing your media...**", parse_mode="Markdown")

    with tempfile.TemporaryDirectory(prefix="tgdl_") as work_dir:
        try:
            files = await process_url(url, headers, work_dir)
            files = [f for f in files if os.path.isfile(f)]
            if not files:
                raise RuntimeError("No media file was produced")

            files = [f for f in files if not is_too_large(f)]
            if not files:
                await status.edit_text(
                    f"❌ **File is too large for Telegram's current bot upload limit ({MAX_FILE_SIZE_MB} MB).**"
                )
                return

            await status.edit_text("📤 **Almost there!**\n\nYour media is ready — sending it now... ✨", parse_mode="Markdown")
            for index, path in enumerate(files[:10]):
                caption = "" if index else "Downloaded by Universal Media Downloader 🚀"
                with open(path, "rb") as media:
                    await update.message.reply_document(document=media, caption=caption)
            await status.delete()
        except Exception:
            logger.exception("Download failed for %s", url)
            await status.edit_text(
                "❌ **Couldn't download this media**\n\n"
                "The link may be private, expired, login-required, protected, or unsupported.\n\n"
                "🔄 **Please check the link and try again.**"
            )


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is not set")

    logger.info("Universal downloader starting | cookies=%s | N_m3u8DL-RE=%s | ffmpeg=%s | streamlink=%s",
                "YES" if COOKIES_PATH else "NO",
                "YES" if shutil.which("N_m3u8DL-RE") else "NO",
                "YES" if shutil.which("ffmpeg") else "NO",
                "YES" if streamlink else "NO")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(120)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
