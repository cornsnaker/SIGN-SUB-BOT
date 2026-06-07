# Sign-Sub Bot runtime image (multi-stage).
# Bundles ffmpeg/ffprobe (subtitle pipeline) and aria2c (leech core) so the
# bot is self-contained.

# ---- Builder: compile wheels (tgcrypto needs a C toolchain) ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---- Runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    WORK_DIR=/data

# System dependencies for the media pipeline and downloader.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        aria2 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install pre-built wheels (no compiler needed in the runtime image).
COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Application code.
COPY signsub ./signsub
COPY SIGNSUB.py ./

# Persistent working directory for downloads / temp assets / session file.
RUN mkdir -p /data
VOLUME ["/data"]

# Config is supplied at runtime via env vars or an --env-file.
# Required: TELEGRAM_API_ID, TELEGRAM_API_HASH, BOT_TOKEN
ENTRYPOINT ["python", "-m", "signsub"]
