import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import profile_feature as pf
from telegram.ext import ApplicationHandlerStop


class ProfileConversation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=42)
        self.status = SimpleNamespace(edit_text=AsyncMock())
        self.message = SimpleNamespace(
            text="https://www.instagram.com/example/?igsh=demo",
            reply_text=AsyncMock(return_value=self.status),
        )
        self.update = SimpleNamespace(message=self.message, effective_user=self.user)
        self.context = SimpleNamespace(user_data={})
        self.posts = [
            pf.Post(code="V1", files=[("https://example.org/1.mp4", None)]),
            pf.Post(code="I1", files=[(None, "https://example.org/1.jpg")]),
            pf.Post(code="V2", files=[("https://example.org/2.mp4", None)]),
        ]
        for obj, key, value in (
            (pf, "bot", bot),
            (bot, "COOKIES_PATH", "mock-cookies"),
            (bot, "ALLOWED_USER_IDS", {42}),
            (pf, "_active", {"uid": None}),
        ):
            self.enterContext(patch.object(obj, key, value))

    async def click(self, suffix):
        buttons = self.status.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        button = next(b for row in buttons for b in row if b.callback_data.endswith(suffix))
        query = SimpleNamespace(data=button.callback_data, answer=AsyncMock(), from_user=self.user)
        await pf.on_callback(SimpleNamespace(callback_query=query), self.context)

    async def scan(self):
        info = {"total": 3, "username": "example", "fullname": "Example", "partial": False}
        with patch.object(pf, "scan_profile", AsyncMock(return_value=(self.posts, info))):
            with self.assertRaises(ApplicationHandlerStop):
                await pf.on_text(self.update, self.context)
        self.assertIn("3", self.status.edit_text.await_args.args[0])

    async def test_all_videos(self):
        await self.scan()
        await self.click(":k:video")
        with patch.object(pf, "run_job", AsyncMock()) as job:
            await self.click(":a:all")
        self.assertEqual([p.code for p in job.await_args.args[1]], ["V1", "V2"])

    async def test_custom_image_count(self):
        await self.scan()
        await self.click(":k:image")
        await self.click(":a:custom")
        with patch.object(pf, "run_job", AsyncMock()) as job:
            self.message.text = "2"
            with self.assertRaises(ApplicationHandlerStop):
                await pf.on_text(self.update, self.context)
            job.assert_not_awaited()
            self.message.text = "1"
            with self.assertRaises(ApplicationHandlerStop):
                await pf.on_text(self.update, self.context)
        self.assertEqual([p.code for p in job.await_args.args[1]], ["I1"])

    async def test_initial_reply_failure_releases_slot(self):
        self.message.reply_text.side_effect = RuntimeError("Telegram unavailable")
        try:
            await pf.begin_scan(self.update, self.context, "example")
        except RuntimeError:
            pass
        self.assertIsNone(pf._active["uid"])

    async def test_profile_url_cannot_fall_through_to_single_media_handler(self):
        self.message.text = "https://www.instagram.com/luckyrajor_/"
        with patch.object(pf, "begin_scan", AsyncMock()) as begin_scan, \
                patch.object(bot, "deliver_media", AsyncMock()) as deliver_media:
            await bot.handle_message(self.update, self.context)
        begin_scan.assert_awaited_once_with(self.update, self.context, "luckyrajor_")
        deliver_media.assert_not_awaited()

    def test_profile_url_normalizes_telegram_invisible_characters(self):
        self.assertEqual(
            pf.extract_username("\u200bhttps://www.instagram.com/luckyrajor_/?igsh=abc\ufeff"),
            "luckyrajor_",
        )


class Delivery(unittest.IsolatedAsyncioTestCase):
    async def test_album_with_singleton_tail(self):
        message = SimpleNamespace(reply_media_group=AsyncMock(), reply_photo=AsyncMock())
        update = SimpleNamespace(message=message)
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for i in range(11):
                path = Path(tmp, f"{i}.jpg")
                path.write_bytes(b"image")
                files.append(str(path))
            await bot.send_image_album(update, files, "caption")
        sizes = [len(c.kwargs["media"]) for c in message.reply_media_group.await_args_list]
        self.assertEqual(sum(sizes) + message.reply_photo.await_count, 11)
        self.assertTrue(all(2 <= size <= 10 for size in sizes))

    async def test_chunked_download_stops_at_limit(self):
        import httpx

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(2):
                    yield b"x" * (1024 * 1024)
                raise AssertionError("read past configured limit")

        async def serve(request):
            return httpx.Response(200, stream=Body(), headers={"Content-Type": "video/mp4"})

        real_client = httpx.AsyncClient
        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(serve), **kwargs)

        with tempfile.TemporaryDirectory() as tmp, patch.object(bot, "MAX_FILE_SIZE_MB", 1), \
                patch.object(bot.httpx, "AsyncClient", side_effect=client), \
                patch.object(bot, "ensure_public_url", AsyncMock()):
            with self.assertRaises(bot.DownloadTooLarge):
                await bot.download_direct("https://example.org/a.mp4", {}, tmp)
            self.assertLessEqual(sum(p.stat().st_size for p in Path(tmp).iterdir()), 1024 * 1024)

    def test_authenticated_requests_require_an_explicit_user(self):
        with patch.object(bot, "COOKIES_PATH", "configured"), patch.object(bot, "ALLOWED_USER_IDS", set()):
            self.assertFalse(bot.is_allowed(42))
        with patch.object(bot, "ALLOWED_USER_IDS", {42}):
            self.assertTrue(bot.is_allowed(42))
            self.assertFalse(bot.is_allowed(43))

    async def test_empty_manifest_results_reach_streamlink(self):
        expected = (["stream.mp4"], "Video")
        with patch.object(bot, "ensure_public_url", AsyncMock()), \
                patch.object(bot, "download_with_n_m3u8dl", return_value=([], "")), \
                patch.object(bot, "download_with_yt_dlp", return_value=([], "")), \
                patch.object(bot, "download_with_streamlink", return_value=expected) as streamlink:
            result = await bot.process_url("https://example.org/live.m3u8", {}, "unused")
        self.assertEqual(result, expected)
        streamlink.assert_called_once()


class ProfileScan(unittest.IsolatedAsyncioTestCase):
    async def test_scan_combines_posts_and_reels_without_duplicates(self):
        def row(code, index, video, image):
            return "\t".join([
                code, str(index), video or "None", image or "None", "2",
                "example", '"Example"', '"caption"',
            ]).encode() + b"\n"

        class Process:
            def __init__(self):
                self.stdout = __import__("asyncio").StreamReader()
                self.stderr = __import__("asyncio").StreamReader()
                for value in (
                    row("REEL1", 1, "https://cdn.example/r.mp4", "https://cdn.example/r.jpg"),
                    row("MIXED1", 1, "https://cdn.example/m.mp4", "https://cdn.example/m.jpg"),
                    row("MIXED1", 2, None, "https://cdn.example/i.jpg"),
                    row("REEL1", 1, "https://cdn.example/r.mp4", "https://cdn.example/r.jpg"),
                ):
                    self.stdout.feed_data(value)
                self.stdout.feed_eof()
                self.stderr.feed_eof()
                self.returncode = 0

            async def wait(self):
                return self.returncode

            def kill(self):
                self.returncode = -9

        session = {"username": "example", "stop": False, "sid": "abc"}
        status = SimpleNamespace(edit_text=AsyncMock())
        with patch.object(pf, "bot", bot), \
                patch.object(pf.shutil, "which", return_value="gallery-dl"), \
                patch.object(pf.asyncio, "create_subprocess_exec", AsyncMock(return_value=Process())) as spawn:
            posts, info = await pf.scan_profile(session, status)

        command = spawn.await_args.args
        self.assertIn("https://www.instagram.com/example/posts/", command)
        self.assertIn("https://www.instagram.com/example/reels/", command)
        self.assertEqual({post.code for post in posts}, {"REEL1", "MIXED1"})
        self.assertEqual(info["total"], 2)
        self.assertEqual(pf.summarize(posts), {"videos": 2, "images": 1, "carousels": 1, "mixed": 1})
        mixed = next(post for post in pf.select_posts(posts, "image", None) if post.code == "MIXED1")
        self.assertEqual(mixed.files, [(None, "https://cdn.example/i.jpg")])

