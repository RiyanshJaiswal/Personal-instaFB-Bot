FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates tar git \
    && rm -rf /var/lib/apt/lists/*

# N_m3u8DL-RE: picks the x64 or arm64 build automatically (works on Oracle Ampere A1 / ARM VMs too).
ARG N_M3U8DL_VERSION=0.6.0-beta
ARG N_M3U8DL_DATE=20260629
RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
      amd64) NARCH=x64 ;; \
      arm64) NARCH=arm64 ;; \
      *) echo "Unsupported architecture: $(dpkg --print-architecture)"; exit 1 ;; \
    esac; \
    curl -fL "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v${N_M3U8DL_VERSION}/N_m3u8DL-RE_v${N_M3U8DL_VERSION}_linux-${NARCH}_${N_M3U8DL_DATE}.tar.gz" \
      -o /tmp/nm3u8.tar.gz; \
    mkdir -p /opt/nm3u8; \
    tar -xzf /tmp/nm3u8.tar.gz -C /opt/nm3u8; \
    BIN="$(find /opt/nm3u8 -type f -name 'N_m3u8DL-RE' | head -n1)"; \
    chmod +x "$BIN"; \
    ln -sf "$BIN" /usr/local/bin/N_m3u8DL-RE; \
    rm -f /tmp/nm3u8.tar.gz

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY bot.py .

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 bot && chown -R bot:bot /app
USER bot

CMD ["python", "bot.py"]
