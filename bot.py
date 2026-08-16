"""
Instagram / Facebook video-download Telegram bot.

Flow: user sends an Instagram or Facebook link -> bot downloads the video
with yt-dlp -> bot sends the video file back in the chat.

Setup instructions are in README.md.
"""

import glob
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid

import yt_dlp
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---- config (env vars, with sane defaults) ---------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")  # required
COOKIES_FILE = os.environ.get("COOKIES_FILE", "cookies.txt")
MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "50"))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "15"))


def prepare_cookies_file() -> str | None:
    """Resolve COOKIES_FILE as either a file path or inline Netscape cookies content.

    This allows Railway Variables to contain the cookies.txt contents directly,
    without committing the sensitive cookies file to GitHub.
    """
    value = COOKIES_FILE.strip()
    if not value:
        return None

    if os.path.isfile(value):
        return value

    looks_like_cookie_content = (
        "# Netscape HTTP Cookie File" in value
        or "# HTTP Cookie File" in value
        or ("instagram.com" in value and "\t" in value)
    )
    if looks_like_cookie_content:
        cookie_path = os.path.join(tempfile.gettempdir(), "instagram_cookies.txt")
        try:
            with open(cookie_path, "w", encoding="utf-8") as fh:
                fh.write(value)
                if not value.endswith("\n"):
                    fh.write("\n")
            os.chmod(cookie_path, 0o600)
            logger.info("Instagram cookies loaded from COOKIES_FILE environment variable")
            return cookie_path
        except Exception:
            logger.exception("Could not create cookies file from COOKIES_FILE variable")
            return None

    logger.warning("COOKIES_FILE is set but no readable cookies file/content was detected")
    return None


COOKIES_PATH = prepare_cookies_file()

URL_PATTERN = re.compile(
    r"(https?://)?(www\.)?(instagram\.com|facebook\.com|fb\.watch)/\S+",
    re.IGNORECASE,
)

_last_request: dict[int, float] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Hey! Welcome!\n\n"
        "📥 Instagram & Facebook Downloader\n\n"
        "🔗 Bas Reel, Video, Story ya Post ka link yahan paste karo.\n"
        "⚡ Main media process karke directly yahin send kar dunga.\n\n"
        "No extra steps. Just paste the link! 🚀"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = update.message.text or ""
    match = URL_PATTERN.search(text)

    if not match:
        await update.message.reply_text(
            "🔗 That doesn't look like a valid link.\n\n"
            "Please send an Instagram or Facebook Reel, Video, Story, or Post link and I'll take care of the rest. ⚡"
        )
        return

    now = time.time()
    wait_left = COOLDOWN_SECONDS - (now - _last_request.get(user_id, 0))
    if wait_left > 0:
        await update.message.reply_text(f"Thoda ruko — {int(wait_left) + 1}s baad try karo.")
        return
    _last_request[user_id] = now

    url = match.group(0)
    status_msg = await update.message.reply_text("⏳ Processing your media...\n\nPlease wait a moment. 🚀")

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_template = os.path.join(tmp_dir, f"{uuid.uuid4().hex}_%(autonumber)03d.%(ext)s")

        ydl_opts = {
            "outtmpl": out_template,
            "format": "best[ext=mp4]/best",
            "quiet": True,
            "no_warnings": True,
        }
        if COOKIES_PATH:
            ydl_opts["cookiefile"] = COOKIES_PATH

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as e:
            logger.warning("yt-dlp couldn't get video for %s: %s", url, e)
            info = {}
        except Exception:
            logger.exception("Unexpected error while downloading %s", url)
            await status_msg.edit_text(
                "❌ Couldn't download this media.\n\n"
                "Please check the link and try again."
            )
            return

        caption = (info.get("description") or "").strip()
        if len(caption) > 1024:
            caption = caption[:1021] + "..."

        video_exts = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
        video_files = sorted(
            (
                f
                for f in glob.glob(os.path.join(tmp_dir, "*"))
                if os.path.splitext(f)[1].lower() in video_exts
            ),
            key=os.path.getmtime,
        )
        logger.info("Found %d video file(s) for %s", len(video_files), url)

        image_dir = os.path.join(tmp_dir, "images")
        os.makedirs(image_dir, exist_ok=True)
        gdl_cmd = [
            "gallery-dl",
            "--filter", "extension not in ('mp4','m4v','mov','mkv','webm')",
            "--write-metadata",
            "-D", image_dir,
            url,
        ]
        if COOKIES_PATH:
            gdl_cmd[1:1] = ["-C", COOKIES_PATH]
        try:
            subprocess.run(gdl_cmd, capture_output=True, timeout=60, check=False)
        except Exception:
            logger.exception("gallery-dl failed for %s", url)

        image_exts = {".jpg", ".jpeg", ".png", ".webp"}
        image_files = sorted(
            (
                f
                for f in glob.glob(os.path.join(image_dir, "**", "*"), recursive=True)
                if os.path.splitext(f)[1].lower() in image_exts
            ),
            key=os.path.getmtime,
        )

        if not caption:
            for meta_path in glob.glob(os.path.join(image_dir, "**", "*.json"), recursive=True):
                try:
                    with open(meta_path, "r", encoding="utf-8") as fh:
                        meta = json.load(fh)
                    caption = (meta.get("description") or "").strip()
                    if caption:
                        if len(caption) > 1024:
                            caption = caption[:1021] + "..."
                        break
                except Exception:
                    continue

        all_files = video_files + image_files
        if not all_files:
            await status_msg.edit_text(
                "❌ Couldn't download this media.\n\n"
                "This may happen if:\n"
                "🔒 The content is private or login-only\n"
                "⏳ The link has expired\n"
                "🚫 Instagram/Facebook is restricting access\n"
                "🔗 The link is invalid\n\n"
                "Please check the link and try again."
            )
            return

        ok_files, skipped = [], 0
        for f in all_files:
            if os.path.getsize(f) / (1024 * 1024) > MAX_FILE_SIZE_MB:
                skipped += 1
            else:
                ok_files.append(f)

        if not ok_files:
            await status_msg.edit_text(
                f"⚠️ The media is larger than the {MAX_FILE_SIZE_MB}MB Telegram limit, so I can't send it."
            )
            return

        await status_msg.edit_text("📤 Almost there!\n\nYour media is ready — sending it now... ✨")
        opened = []
        try:
            if len(ok_files) == 1:
                f = ok_files[0]
                with open(f, "rb") as media_file:
                    if os.path.splitext(f)[1].lower() in image_exts:
                        await update.message.reply_photo(photo=media_file, caption=caption or None)
                    else:
                        await update.message.reply_video(video=media_file, caption=caption or None)
            else:
                media = []
                for i, f in enumerate(ok_files[:10]):
                    fh = open(f, "rb")
                    opened.append(fh)
                    cap = caption if i == 0 else None
                    if os.path.splitext(f)[1].lower() in image_exts:
                        media.append(InputMediaPhoto(media=fh, caption=cap))
                    else:
                        media.append(InputMediaVideo(media=fh, caption=cap))
                await update.message.reply_media_group(media=media)

            await status_msg.delete()
            if skipped:
                await update.message.reply_text(
                    f"⚠️ {skipped} file(s) exceeded the {MAX_FILE_SIZE_MB}MB Telegram limit and were skipped."
                )
        except Exception:
            logger.exception("Unexpected error while uploading to Telegram")
            await status_msg.edit_text(
                "❌ The download finished, but I couldn't send the media to Telegram.\n\nPlease try again."
            )
        finally:
            for fh in opened:
                fh.close()


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN set nahi hai. @BotFather se token lo, phir Railway Variables mein BOT_TOKEN set karo."
        )

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

    logger.info("Bot started, polling...")
    app.run_polling()


if __name__ == "__main__":
    main()