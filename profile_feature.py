"""Instagram profile scan + bulk download.

Flow (inside Telegram):
  1. User sends an Instagram *profile* link.
  2. The bot lists the profile's posts with gallery-dl (metadata only), counts them and splits them into
     reels/videos vs image posts (carousels included).
  3. The user taps what to download (reels/videos, images, or both) and how many (all, 10/25/50/100, custom).
  4. The bot downloads and sends them one by one, newest first, with a Stop button.

This module is wired in from bot.py via register(); `bot` is the bot module (passed in to avoid a circular import).
"""

import asyncio
import collections
import json
import logging
import os
import random
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

logger = logging.getLogger(__name__)
bot = None  # the bot module, set by register()

SCAN_LIMIT = int(os.environ.get("PROFILE_SCAN_LIMIT", "500"))          # max posts looked at per profile
SCAN_SLEEP = os.environ.get("PROFILE_SCAN_SLEEP", "3.0-6.0")           # seconds between Instagram requests (scan)
SCAN_TIMEOUT = int(os.environ.get("PROFILE_SCAN_TIMEOUT", "900"))      # hard limit for one scan
PAUSE_SECONDS = float(os.environ.get("PROFILE_PAUSE_SECONDS", "2"))    # pause between posts while downloading
FAIL_STREAK_LIMIT = int(os.environ.get("PROFILE_FAIL_STREAK", "5"))    # stop after this many failures in a row
SESSION_TTL = 30 * 60                                                  # menus expire after 30 minutes

RESERVED = {
    "p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "s", "share", "about", "legal",
    "developer", "directory", "web", "challenge", "oauth", "emails", "locations", "api", "static", "privacy",
    "terms", "ar", "download",
}
PROFILE_RE = re.compile(
    r"^https?://(?:www\.|m\.)?instagram\.com/(?:_u/)?(?P<user>[A-Za-z0-9._]{1,30})/?"
    r"(?:(?:posts|reels|tagged)/?)?(?:\?\S*)?(?:#\S*)?$",
    re.I,
)

# One line per media file: shortcode, index in post, video url, image url, total posts, username, name(json), caption(json)
PRINT_FORMAT = "\t".join([
    "{post_shortcode}", "{num}", "{video_url!s}", "{display_url!s}",
    "{user[count_media]!s}", "{username}", "{fullname!j}", "{description!j}",
])

_active = {"uid": None}  # only one scan/download job at a time, to protect the Instagram session


class ScanError(Exception):
    """Scan failed; the message is safe to show to the user."""


@dataclass
class Post:
    code: str
    files: list = field(default_factory=list)  # [(video_url | None, image_url | None)]
    description: str = ""

    @property
    def kind(self) -> str:
        videos = sum(1 for v, _ in self.files if v)
        if videos and videos == len(self.files):
            return "video"
        return "mixed" if videos else "image"

    @property
    def link(self) -> str:
        return f"https://www.instagram.com/p/{self.code}/"


# ---------------------------------------------------------------- parsing helpers

def extract_username(text: str) -> str | None:
    """Return the username if the whole message is just an Instagram profile link."""
    parts = text.strip().split()
    if len(parts) != 1:
        return None
    m = PROFILE_RE.match(parts[0])
    if not m or m.group("user").lower() in RESERVED:
        return None
    return m.group("user")


def _none(value: str) -> str | None:
    return None if value in ("None", "", "null") else value


def parse_line(line: str) -> dict | None:
    parts = line.rstrip("\r\n").split("\t")
    if len(parts) != 8 or not parts[0] or parts[0] == "None":
        return None
    code, _num, vurl, iurl, total, username, fullname, desc = parts
    try:
        description = json.loads(desc)
    except ValueError:
        description = ""
    try:
        name = json.loads(fullname)
    except ValueError:
        name = ""
    return {
        "code": code, "video_url": _none(vurl), "image_url": _none(iurl),
        "total": int(total) if total.isdigit() else None,
        "username": _none(username), "fullname": name if isinstance(name, str) else "",
        "description": description if isinstance(description, str) else "",
    }


def summarize(posts: list) -> dict:
    videos = [p for p in posts if p.kind == "video"]
    images = [p for p in posts if p.kind in ("image", "mixed")]
    return {
        "videos": len(videos), "images": len(images),
        "carousels": sum(1 for p in images if len(p.files) > 1),
        "mixed": sum(1 for p in posts if p.kind == "mixed"),
    }


def select_posts(posts: list, kind: str, amount: int | None) -> list:
    if kind == "video":
        pool = [p for p in posts if p.kind == "video"]
    elif kind == "image":
        pool = [p for p in posts if p.kind in ("image", "mixed")]
    else:
        pool = list(posts)
    return pool if amount is None else pool[:amount]


def explain_failure(tail: str) -> str:
    t = tail.lower()
    if "429" in t or "too many requests" in t or "rate limit" in t or "rate-limit" in t:
        return "Instagram ne abhi rate-limit laga diya hai. 30-60 minute baad dobara try karo."
    if "404" in t or "not found" in t or "notfound" in t:
        return "Ye profile nahi mila. Username check karo."
    if "private" in t or "login" in t or "401" in t or "403" in t or "authoriz" in t or "checkpoint" in t or "challenge" in t:
        return "Profile private hai ya Instagram cookies expire ho gayi hain."
    return "Profile scan fail ho gaya. Server logs me detail hai."


def build_caption(post: Post) -> str:
    text = bot.clean_caption(post.description, "")
    room = 1024 - len(post.link) - 2
    if len(text) > room:
        text = text[: max(room - 1, 0)] + "…"
    return f"{text}\n\n{post.link}" if text else post.link


# ---------------------------------------------------------------- telegram helpers

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def kb_stop(sid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("⏹ Stop", f"pf:{sid}:stop")]])


def kb_kind(sid: str, stats: dict) -> InlineKeyboardMarkup:
    rows = []
    if stats["videos"]:
        rows.append([_btn(f"🎬 Reels/Videos ({stats['videos']})", f"pf:{sid}:k:video")])
    if stats["images"]:
        rows.append([_btn(f"🖼 Images ({stats['images']})", f"pf:{sid}:k:image")])
    if stats["videos"] and stats["images"]:
        rows.append([_btn(f"📦 Dono ({stats['videos'] + stats['images']})", f"pf:{sid}:k:both")])
    rows.append([_btn("❌ Cancel", f"pf:{sid}:x")])
    return InlineKeyboardMarkup(rows)


def kb_amount(sid: str, available: int) -> InlineKeyboardMarkup:
    presets = [n for n in (10, 25, 50, 100) if n < available]
    rows = []
    if presets:
        rows.append([_btn(str(n), f"pf:{sid}:a:{n}") for n in presets])
    rows.append([_btn(f"♾ Saari ({available})", f"pf:{sid}:a:all")])
    rows.append([_btn("✏️ Custom number", f"pf:{sid}:a:custom")])
    rows.append([_btn("⬅️ Peeche", f"pf:{sid}:back"), _btn("❌ Cancel", f"pf:{sid}:x")])
    return InlineKeyboardMarkup(rows)


async def safe_edit(msg, text: str, markup=None) -> None:
    try:
        await msg.edit_text(text, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            logger.debug("edit failed: %s", exc)
    except RetryAfter:
        pass
    except Exception as exc:  # status edits must never break the job
        logger.debug("edit failed: %s", exc)


async def _with_flood_retry(factory):
    for attempt in range(3):
        try:
            return await factory()
        except RetryAfter as exc:
            if attempt == 2:
                raise
            await asyncio.sleep(float(exc.retry_after) + 1)


# ---------------------------------------------------------------- scanning

async def scan_profile(session: dict, status_msg) -> tuple[list, dict]:
    """Stream gallery-dl's metadata output and build the post list. Returns (posts, info)."""
    binary = shutil.which("gallery-dl")
    if not binary:
        raise ScanError("gallery-dl server pe install nahi hai.")
    username = session["username"]
    cmd = [
        binary, "--cookies", bot.COOKIES_PATH,
        "-o", f"max-posts={SCAN_LIMIT}", "-o", f"sleep-request={SCAN_SLEEP}",
        "-o", "sleep-429=30", "-o", "retries=2",
        "-N", PRINT_FORMAT, f"https://www.instagram.com/{username}/posts/",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1 << 20,
    )
    queue: asyncio.Queue = asyncio.Queue()
    stderr_tail: collections.deque = collections.deque(maxlen=25)

    async def pump_out():
        while True:
            line = await proc.stdout.readline()
            await queue.put(line)
            if not line:
                return

    async def pump_err():
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            stderr_tail.append(line.decode(errors="replace").rstrip())

    pumps = [asyncio.create_task(pump_out()), asyncio.create_task(pump_err())]
    posts: list[Post] = []
    info = {"total": None, "username": username, "fullname": "", "partial": False}
    current: Post | None = None
    started = last_edit = time.monotonic()

    async def progress():
        nonlocal last_edit
        if time.monotonic() - last_edit < 3:
            return
        last_edit = time.monotonic()
        total = f" / {info['total']}" if info["total"] else ""
        await safe_edit(
            status_msg,
            f"🔍 @{username} scan ho raha hai...\n📄 {len(posts)}{total} posts mile\n⏱ {int(time.monotonic() - started)}s",
            kb_stop(session["sid"]),
        )

    try:
        while True:
            if session["stop"]:
                raise ScanError("Scan cancel kar diya.")
            if time.monotonic() - started > SCAN_TIMEOUT:
                info["partial"] = True
                break
            try:
                raw = await asyncio.wait_for(queue.get(), 2)
            except asyncio.TimeoutError:
                await progress()
                continue
            if not raw:
                break
            row = parse_line(raw.decode(errors="replace"))
            if not row:
                continue
            if row["total"] and not info["total"]:
                info["total"] = row["total"]
            if row["fullname"] and not info["fullname"]:
                info["fullname"] = row["fullname"]
            if current is None or current.code != row["code"]:
                current = Post(code=row["code"], description=row["description"])
                posts.append(current)
            current.files.append((row["video_url"], row["image_url"]))
            await progress()
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        for task in pumps:
            task.cancel()

    tail = "\n".join(stderr_tail)
    if proc.returncode not in (0, None) and not info["partial"]:
        logger.warning("gallery-dl exit=%s for @%s: %s", proc.returncode, username, tail[-1500:])
        if not posts:
            raise ScanError(explain_failure(tail))
        info["partial"] = True
    if not posts:
        logger.warning("gallery-dl returned no posts for @%s: %s", username, tail[-1500:])
        raise ScanError("Is profile me koi post nahi mili (ya account private hai).")
    return posts, info


def summary_text(session: dict) -> str:
    stats, info = summarize(session["posts"]), session["info"]
    name = f" ({info['fullname']})" if info.get("fullname") else ""
    lines = [f"📊 @{session['username']}{name}"]
    if info.get("total"):
        lines.append(f"📌 Total posts: {info['total']}")
    scanned = len(session["posts"])
    note = ""
    if info.get("total") and scanned < info["total"]:
        note = " (latest posts, baaki skip)" if scanned >= SCAN_LIMIT else " (kuch posts available nahi the)"
    lines.append(f"🔎 Scan hui: {scanned} posts{note}")
    if info.get("partial"):
        lines.append("⚠️ Scan beech me ruk gaya (rate-limit/timeout), jitna mila utna dikha raha hu.")
    lines += [
        "",
        f"🎬 Reels/Videos: {stats['videos']}",
        f"🖼 Image posts: {stats['images']}" + (f" (carousel: {stats['carousels']})" if stats["carousels"] else ""),
    ]
    if stats["mixed"]:
        lines.append(f"ℹ️ {stats['mixed']} carousel me photo + video dono hain (Images me ginti hui).")
    lines += ["", "Kya download karna hai?"]
    return "\n".join(lines)


# ---------------------------------------------------------------- downloading

async def fetch_post(post: Post, workdir: str) -> list[str]:
    headers = {"User-Agent": bot.DEFAULT_USER_AGENT}
    if post.kind == "video" and len(post.files) == 1:
        try:  # proven path: yt-dlp merges audio+video correctly
            files, _ = await asyncio.to_thread(bot.download_with_yt_dlp, post.link, headers, workdir)
            files = [f for f in files if os.path.isfile(f)]
            if files:
                return files
        except Exception as exc:
            logger.info("yt-dlp failed for %s, using direct CDN url: %s", post.code, exc)
    paths: list[str] = []
    for index, (video_url, image_url) in enumerate(post.files, 1):
        url = video_url or image_url
        if not url:
            continue
        sub = os.path.join(workdir, f"{index:02d}")
        os.makedirs(sub, exist_ok=True)
        got, _ = await bot.download_direct(url, headers, sub)
        paths.extend(got)
    return paths


async def send_files(origin, files: list[str], caption: str) -> None:
    target = SimpleNamespace(message=origin)  # bot.send_* expect update.message.reply_*()
    images = [f for f in files if bot.is_image(f)]
    others = [f for f in files if not bot.is_image(f)]
    first = True
    if len(images) > 1:
        await _with_flood_retry(lambda: bot.send_image_album(target, images, caption))
        first = False
    elif images:
        await _with_flood_retry(lambda: bot.send_single(target, images[0], caption))
        first = False
    for path in others:
        text = caption if first else ""
        await _with_flood_retry(lambda p=path, c=text: bot.send_single(target, p, c))
        first = False


async def run_job(session: dict, posts: list) -> None:
    status, origin, sid = session["status"], session["origin"], session["sid"]
    ok = failed = streak = 0
    failed_links: list[str] = []
    aborted = ""
    last_edit = 0.0
    label = {"video": "Reels/Videos", "image": "Images", "both": "Posts"}[session["kind"]]
    total = len(posts)

    async def show(force: bool = False):
        nonlocal last_edit
        if not force and time.monotonic() - last_edit < 2.5:
            return
        last_edit = time.monotonic()
        extra = f"\n⚠️ Fail: {failed}" if failed else ""
        await safe_edit(status, f"⬇️ @{session['username']} - {label}\n✅ {ok}/{total} bheje{extra}", kb_stop(sid))

    await show(force=True)
    for post in posts:
        if session["stop"]:
            break
        try:
            async with bot._job_slots:
                with tempfile.TemporaryDirectory(prefix="pf_") as workdir:
                    files = await fetch_post(post, workdir)
                    if not files:
                        raise RuntimeError("no media files")
                    await send_files(origin, files, build_caption(post))
            ok += 1
            streak = 0
        except Exception as exc:
            failed += 1
            streak += 1
            failed_links.append(post.link)
            logger.warning("profile item %s failed: %s", post.code, exc)
            if streak >= FAIL_STREAK_LIMIT:
                aborted = f"Lagatar {streak} posts fail hue, isliye ruk gaya (cookies expire ya Instagram block ho sakta hai)."
                break
        await show()
        await asyncio.sleep(PAUSE_SECONDS + random.random())

    stopped = session["stop"]
    head = "⏹ Stop kar diya." if stopped else ("⚠️ " + aborted if aborted else "✅ Ho gaya!")
    text = f"{head}\n@{session['username']} - {label}: {ok}/{total} bheje"
    if failed:
        text += f", {failed} fail"
    await safe_edit(status, text)
    if failed_links:
        shown = "\n".join(failed_links[:10])
        more = f"\n... aur {len(failed_links) - 10}" if len(failed_links) > 10 else ""
        try:
            await origin.reply_text(f"Ye posts nahi aa paye:\n{shown}{more}", disable_web_page_preview=True)
        except Exception:
            pass


async def start_job(context: ContextTypes.DEFAULT_TYPE, session: dict, amount: int | None) -> None:
    posts = select_posts(session["posts"], session["kind"], amount)
    if not posts:
        await safe_edit(session["status"], "Is category me koi post nahi mili.")
        context.user_data.pop("pf", None)
        return
    if _active["uid"] is not None:
        await safe_edit(session["status"], "Abhi ek aur profile job chal raha hai. Thodi der baad dobara try karo.")
        return
    _active["uid"] = session["uid"]
    session.update(state="running", stop=False)
    try:
        await run_job(session, posts)
    finally:
        _active["uid"] = None
        context.user_data.pop("pf", None)


# ---------------------------------------------------------------- telegram handlers

async def begin_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, username: str) -> None:
    msg, uid = update.message, update.effective_user.id
    if not bot.COOKIES_PATH:
        await msg.reply_text("Profile scan ke liye Instagram cookies zaroori hain (server pe cookies=NO hai).")
        return
    if _active["uid"] is not None:
        await msg.reply_text("Abhi ek profile scan/download chal raha hai. Thodi der baad dobara bhejo.")
        return
    _active["uid"] = uid
    sid = secrets.token_hex(3)
    status = await msg.reply_text(
        f"🔍 @{username} check ho raha hai...\n(Posts ginne me kuch minute lag sakte hain)", reply_markup=kb_stop(sid),
    )
    session = {"sid": sid, "uid": uid, "username": username, "status": status, "origin": msg, "state": "scanning",
               "stop": False, "posts": [], "info": {}, "kind": None, "touched": time.time()}
    context.user_data["pf"] = session
    try:
        posts, info = await scan_profile(session, status)
        session.update(posts=posts, info=info, state="choose_kind", touched=time.time())
        await safe_edit(status, summary_text(session), kb_kind(sid, summarize(posts)))
    except ScanError as exc:
        context.user_data.pop("pf", None)
        await safe_edit(status, f"❌ {exc}")
    except Exception:
        logger.exception("profile scan crashed for @%s", username)
        context.user_data.pop("pf", None)
        await safe_edit(status, "❌ Profile scan me error aaya. Server logs dekho.")
    finally:
        _active["uid"] = None


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not msg.text or not update.effective_user:
        return
    uid, text = update.effective_user.id, msg.text.strip()
    if not bot.is_allowed(uid):
        return
    session = context.user_data.get("pf")
    if session and session.get("state") == "await_number" and text.isdigit():
        available = len(select_posts(session["posts"], session["kind"], None))
        amount = int(text)
        if amount < 1 or amount > available:
            await msg.reply_text(f"1 se {available} ke beech number bhejo.")
        else:
            await start_job(context, session, amount)
        raise ApplicationHandlerStop
    username = extract_username(text)
    if username:
        await begin_scan(update, context, username)
        raise ApplicationHandlerStop


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) < 3 or not bot.is_allowed(query.from_user.id):
        await query.answer()
        return
    sid, action = parts[1], parts[2]
    session = context.user_data.get("pf")
    expired = session is not None and session["state"] != "running" and time.time() - session["touched"] > SESSION_TTL
    if not session or session["sid"] != sid or expired:
        await query.answer("Ye menu purana ho gaya. Profile link dobara bhejo.", show_alert=True)
        return
    session["touched"] = time.time()
    status = session["status"]

    if action == "stop":
        session["stop"] = True
        await query.answer("Ruk raha hu...")
        return
    if session["state"] == "running" or session["state"] == "scanning":
        await query.answer("Abhi kaam chal raha hai, Stop dabao.")
        return
    await query.answer()
    if action == "x":
        context.user_data.pop("pf", None)
        await safe_edit(status, "Cancel kar diya.")
    elif action == "back":
        session["state"] = "choose_kind"
        await safe_edit(status, summary_text(session), kb_kind(sid, summarize(session["posts"])))
    elif action == "k" and len(parts) == 4 and parts[3] in ("video", "image", "both"):
        session.update(kind=parts[3], state="choose_amount")
        available = len(select_posts(session["posts"], session["kind"], None))
        names = {"video": "Reels/Videos", "image": "Image posts", "both": "Posts (reels + images)"}
        await safe_edit(status, f"{names[parts[3]]}: {available} mile.\nKitne download karu?", kb_amount(sid, available))
    elif action == "a" and len(parts) == 4 and session.get("kind"):
        available = len(select_posts(session["posts"], session["kind"], None))
        if parts[3] == "custom":
            session["state"] = "await_number"
            await safe_edit(status, f"Kitne download karu? 1 se {available} ke beech number bhejo.",
                            InlineKeyboardMarkup([[_btn("⬅️ Peeche", f"pf:{sid}:back"), _btn("❌ Cancel", f"pf:{sid}:x")]]))
        else:
            amount = None if parts[3] == "all" else int(parts[3]) if parts[3].isdigit() else None
            await start_job(context, session, amount)


async def on_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not bot.is_allowed(update.effective_user.id):
        return
    session = context.user_data.get("pf")
    if session and session.get("state") in ("scanning", "running"):
        session["stop"] = True
        await update.message.reply_text("Ruk raha hu...")
    else:
        await update.message.reply_text("Abhi koi profile job nahi chal raha.")


def register(app, bot_module) -> None:
    global bot
    bot = bot_module
    # group -1 runs before the normal link handler; on_text raises ApplicationHandlerStop when it handles a message
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text), group=-1)
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^pf:"))
    app.add_handler(CommandHandler("stop", on_stop_command))
