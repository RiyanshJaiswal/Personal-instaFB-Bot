FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates tar git \
    && rm -rf /var/lib/apt/lists/*

# N_m3u8DL-RE v0.6.0-beta: current Linux x64 static build.
ARG N_M3U8DL_VERSION=0.6.0-beta
ARG N_M3U8DL_DATE=20260629
RUN curl -fL "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v${N_M3U8DL_VERSION}/N_m3u8DL-RE_v${N_M3U8DL_VERSION}_linux-x64_${N_M3U8DL_DATE}.tar.gz" \
    -o /tmp/nm3u8.tar.gz \
    && mkdir -p /opt/nm3u8 \
    && tar -xzf /tmp/nm3u8.tar.gz -C /opt/nm3u8 \
    && find /opt/nm3u8 -type f -name 'N_m3u8DL-RE' -exec chmod +x {} \; \
    && ln -sf "$(find /opt/nm3u8 -type f -name 'N_m3u8DL-RE' | head -n1)" /usr/local/bin/N_m3u8DL-RE \
    && rm -f /tmp/nm3u8.tar.gz

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY bot.py profile_feature.py ./

# Run as non-root; UID 1001 matches ubuntu on the target Oracle VM and its mounted cookies file.
RUN useradd -m --uid 1001 botuser
USER botuser

CMD ["python", "bot.py"]

