# Universal Media Downloader — Telegram Bot

Send a URL to Telegram and the bot downloads the media server-side and sends it back.
Built for personal use (Instagram, Facebook, Threads and generic video/stream links).

## Downloader stack

1. **yt-dlp** — primary extractor (social sites, many HLS/DASH cases).
2. **N_m3u8DL-RE** — dedicated HLS/M3U8/DASH fallback.
3. **gallery-dl** — image/carousel fallback for Instagram, Facebook and Threads.
4. **Streamlink** — streaming fallback.
5. **FFmpeg** — remux / transcode (only when the video isn't already Telegram-compatible).
6. **httpx** — direct media-file downloads.

## Telegram usage

```text
https://example.com/video
```

For a stream that needs browser-style headers:

```text
https://example.com/video.m3u8
Referer: https://example.com/watch
User-Agent: Mozilla/5.0
```

`Cookie`, `Authorization` and `Host` headers are intentionally not accepted from Telegram text.
Session data belongs in the `COOKIES_FILE` environment variable.

## Environment variables

| Variable | Default | Purpose |
|---|---:|---|
| `BOT_TOKEN` | required | Telegram BotFather token |
| `ALLOWED_USERS` | **required** | Comma-separated Telegram numeric user IDs allowed to use the bot (get yours from `@userinfobot`). The bot refuses to start without it. |
| `COOKIES_FILE` | empty | Netscape `cookies.txt` path or inline cookie-file content |
| `MAX_FILE_SIZE_MB` | `50` | Max file size (Telegram's standard Bot API limit is 50 MB) |
| `COOLDOWN_SECONDS` | `5` | Per-user request cooldown |
| `DOWNLOAD_TIMEOUT` | `1800` | Downloader process timeout in seconds |
| `MAX_CONCURRENT_JOBS` | `2` | Parallel downloads (lower this on small VMs) |
| `MAX_PLAYLIST_ITEMS` | `10` | Max items fetched from a playlist/carousel |
| `DEFAULT_USER_AGENT` | Chrome-like UA | Default HTTP User-Agent |

See `.env.example`. Never commit `.env`, cookies, or bot tokens.

## Security notes

- **Allowlist:** only users in `ALLOWED_USERS` get a response; everyone else is silently ignored.
- **SSRF guard:** URLs resolving to private, loopback, link-local (incl. cloud metadata `169.254.169.254`) or reserved IPs are rejected, and direct-download redirects are re-validated on every hop. yt-dlp/gallery-dl follow their own redirects and DNS rebinding isn't covered, so on cloud VMs also block the metadata endpoint at the firewall:
  ```bash
  sudo iptables -I OUTPUT -d 169.254.169.254 -j REJECT
  ```
- **Limits:** direct downloads are size-capped while streaming; yt-dlp has `max_filesize` and a playlist item cap.
- The container runs as a non-root user.

## Deploy on an Oracle Cloud VM (x64 or ARM/Ampere)

The Dockerfile picks the right N_m3u8DL-RE build for the CPU architecture. No inbound ports are needed (the bot uses polling).

```bash
git clone https://github.com/RiyanshJaiswal/Personal-instaFB-Bot.git
cd Personal-instaFB-Bot
cp .env.example /home/ubuntu/bot.env && chmod 600 /home/ubuntu/bot.env   # then edit it
docker build -t igbot .
docker run -d --restart unless-stopped --env-file /home/ubuntu/bot.env --name igbot igbot
docker logs -f igbot
```

Update / redeploy (also pulls the latest yt-dlp):

```bash
git pull && docker build --no-cache -t igbot . && docker rm -f igbot \
  && docker run -d --restart unless-stopped --env-file /home/ubuntu/bot.env --name igbot igbot
```

Housekeeping: `docker system prune -f` occasionally (weekly cron) to reclaim disk.

> Instagram/Facebook often challenge or block datacenter IPs (Oracle included). Valid cookies help, but some links may still fail. That's the platform, not the bot.

## Local run

```bash
pip install -r requirements.txt   # plus ffmpeg on your system
export BOT_TOKEN="YOUR_TOKEN"
export ALLOWED_USERS="123456789"
python bot.py
```

## Known limitation

A media URL copied from another machine may fail when replayed from a server if the provider binds the request to the original IP, cookies, Referer, or other headers.

Use this only for media you are authorized to download.
