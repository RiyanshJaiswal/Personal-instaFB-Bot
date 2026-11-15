# Universal Media Downloader — Telegram Bot

Send a URL to Telegram and the bot downloads the media server-side and sends it back.

## Downloader stack

The bot uses multiple engines instead of relying on one extractor:

1. **yt-dlp** — primary website/video extractor, including supported social/video sites and many HLS/DASH cases.
2. **N_m3u8DL-RE v0.6.0-beta** — dedicated HLS/M3U8/DASH/MSS fallback.
3. **Streamlink** — streaming fallback.
4. **FFmpeg** — mux/remux/format handling.
5. **httpx** — direct media-file downloads.

The Docker image installs FFmpeg and the Linux x64 N_m3u8DL-RE build automatically.

## Telegram usage

Send simply:

```text
https://example.com/video
```

For a stream that requires browser-style headers, the bot also accepts:

```text
https://example.com/video.m3u8
Referer: https://example.com/watch
User-Agent: Mozilla/5.0
```

`Cookie` and `Authorization` headers are intentionally not accepted from Telegram text. Sensitive session data should stay in the secure `COOKIES_FILE` environment variable.

## Environment variables

| Variable | Default | Purpose |
|---|---:|---|
| `BOT_TOKEN` | required | Telegram BotFather token |
| `ALLOWED_USER_IDS` | empty (open to all) | Comma-separated Telegram user IDs allowed to use the bot — **set this** |
| `COOKIES_FILE` | empty | Netscape `cookies.txt` path or inline cookie-file content |
| `MAX_FILE_SIZE_MB` | `50` | Maximum file sent through the normal Telegram Bot API |
| `COOLDOWN_SECONDS` | `5` | Per-user request cooldown |
| `DOWNLOAD_TIMEOUT` | `1800` | Downloader process timeout in seconds |
| `DEFAULT_USER_AGENT` | Chrome-like UA | Default HTTP User-Agent |

### Cookies

`COOKIES_FILE` can be a path to a `cookies.txt` file or the complete Netscape/Mozilla cookie-file content. The bot writes it to a temporary file at startup and never logs the cookie values.

The cookie file must start with either:

```text
# Netscape HTTP Cookie File
```

or

```text
# HTTP Cookie File
```

Never commit live cookies or bot tokens to GitHub.

## Local run

Install Python dependencies and FFmpeg, then:

```bash
export BOT_TOKEN="YOUR_TOKEN"
python bot.py
```

## Deploy on an Oracle Cloud VM (Docker)

```bash
git clone https://github.com/RiyanshJaiswal/Personal-instaFB-Bot.git
cd Personal-instaFB-Bot
docker build -t tg-media-bot .
docker run -d --name tg-media-bot --restart unless-stopped \
  -e BOT_TOKEN="YOUR_TOKEN" \
  -e ALLOWED_USER_IDS="123456789" \
  tg-media-bot
```

To use cookies, keep `cookies.txt` on the VM (never commit it) and mount it:

```bash
docker run -d --name tg-media-bot --restart unless-stopped \
  -e BOT_TOKEN="YOUR_TOKEN" \
  -e ALLOWED_USER_IDS="123456789" \
  -e COOKIES_FILE=/data/cookies.txt \
  -v /home/ubuntu/cookies.txt:/data/cookies.txt:ro \
  tg-media-bot
```

Update the container after pulling changes. Reuse the same environment variables and cookie mount from the initial `docker run` command:

```bash
git pull
docker build -t tg-media-bot .
docker rm -f tg-media-bot
docker run -d --name tg-media-bot --restart unless-stopped \
  -e BOT_TOKEN="YOUR_TOKEN" \
  -e ALLOWED_USER_IDS="123456789" \
  tg-media-bot
```

If you use cookies, include the same `COOKIES_FILE` and read-only volume mount shown above. Logs: `docker logs -f tg-media-bot`.

The Docker image installs Python 3.13, FFmpeg, N_m3u8DL-RE v0.6.0-beta (Linux x64) and the Python dependencies. If your Oracle VM is ARM (Ampere A1), the x64 N_m3u8DL-RE binary will not run; use an AMD (x86) VM shape or install the arm64 build.

## Important limitation

A media URL copied from another machine may fail when replayed from this server if the provider binds the request to the original IP, cookies, Referer, or other HTTP headers. For that reason the next desktop-helper phase will send the relevant URL and request metadata from the laptop to this bot.

Use this only for media you are authorized to download.

