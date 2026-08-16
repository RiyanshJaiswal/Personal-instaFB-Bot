# Instagram / Facebook Video Downloader — Telegram Bot

Link bhejo, video wapas milega. Backend mein `yt-dlp` use hota hai (koi official
IG/FB API nahi hai jo ye karta ho — dono platforms unofficial/scraping route se
hi kaam karte hain, isliye reliability 100% guaranteed nahi hai).

## 1. Bot token lo

1. Telegram par **@BotFather** ko message karo.
2. `/newbot` bhejo, naam aur username choose karo.
3. Jo token milega (kuch aisa dikhega: `123456789:ABCdefGhIJKlmNoPQRsTUVwxyz`) — usko save karo.

## 2. Install karo

```bash
# ffmpeg zaroori hai (audio+video merge ke liye kabhi kabhi)
sudo apt install ffmpeg        # Linux
brew install ffmpeg            # Mac

pip install -r requirements.txt
```

## 3. (Recommended) Instagram cookies set karo

2026 mein Instagram ka zyadatar content bina login ke fail ho jaata hai. Reliable
kaam karwane ke liye:

1. Chrome/Firefox mein ek **throwaway/secondary** Instagram account se login karo
   (apna main account mat use karo — heavy automated use se account action-block
   ho sakta hai).
2. "Get cookies.txt LOCALLY" jaisi browser extension se instagram.com ki cookies
   export karo, `cookies.txt` naam se is folder mein rakho.
3. Bot khud detect kar lega agar `cookies.txt` maujood hai.

Facebook public videos usually bina login ke bhi chal jaate hain.

## 4. Run karo

```bash
export BOT_TOKEN="yahan_apna_token_daalo"
python3 bot.py
```

Telegram par apne bot ko koi bhi Instagram/Facebook video ka link bhejo.

## Free deploy (Railway) — low-traffic personal use

This bot uses **polling**, so it needs to stay running — that rules out
serverless/sleep-after-idle free tiers (Render, Vercel). Railway works well
for a low-traffic personal bot:

1. Push this folder (`bot.py`, `requirements.txt`, `Dockerfile`) to a **GitHub repo**.
2. Go to [railway.app](https://railway.app) → sign up (no card needed for the trial) →
   **New Project → Deploy from GitHub repo** → pick your repo.
3. Railway detects the `Dockerfile` automatically and builds it (ffmpeg included).
4. In the service's **Variables** tab, add `BOT_TOKEN` (and `COOKIES_FILE` /
   `COOLDOWN_SECONDS` if you want non-default values).
5. Deploy. Check the **Logs** tab for `Bot started, polling...`.
6. If you're using Instagram cookies, add `cookies.txt` to the repo (or Railway's
   volume) so `COOKIES_FILE` can find it — don't commit it to a **public** repo,
   since it contains a live login session.

At ~100 requests/day this comfortably stays inside Railway's free monthly credit,
so it should cost nothing.

**More permanent free alternative:** an Oracle Cloud "Always Free" ARM VM — genuinely
free forever (no credit that runs out), but you set it up as a real Linux server
yourself (SSH in, install Docker, `docker run` this image, keep it alive with
`systemd` or `docker restart=always`). More setup work, more control.

Avoid the many small "free Telegram bot hosting" sites floating around — handing
your bot token to an unfamiliar third-party host is a real risk, since the token
gives full control of the bot.

## Config (optional env vars)

| Variable | Default | Kya karta hai |
|---|---|---|
| `BOT_TOKEN` | — (required) | BotFather wala token |
| `COOKIES_FILE` | `cookies.txt` | Instagram cookies file ka path |
| `MAX_FILE_SIZE_MB` | `50` | Telegram bot API ki hard limit hi 50MB hai |
| `COOLDOWN_SECONDS` | `15` | Per-user cooldown, taaki IG/FB ban na kare |

## Limitations / good-to-know

- **Sirf videos/reels** — photo-only posts support nahi hain.
- **50MB file limit** — Telegram Bot API ka hard cap hai. Local Bot API server
  self-host karke 2GB tak badha sakte ho, but ye setup extra kaam hai — abhi
  isme nahi kiya.
- **Ye break ho sakta hai** — Instagram/Facebook apna internal structure change
  karte rehte hain, to kabhi kabhi `yt-dlp` update karna padega:
  `pip install -U yt-dlp`
- **ToS/copyright**: scraping Instagram/Facebook ke ToS ke against hai. Apna
  content ya jisko download karne ki permission ho, uske liye use karo — doosron
  ka content redistribute/repost mat karo.
