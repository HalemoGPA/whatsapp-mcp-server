# syntax=docker/dockerfile:1.7

# --- builder ----------------------------------------------------------------
# Resolve + install deps into a dedicated venv from uv.lock (frozen, no resolve).
# Builder stage is discarded - none of uv or the build cache ships in the final image.
FROM ghcr.io/astral-sh/uv:0.5-python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_INSTALLER_METADATA=1

WORKDIR /srv

# Deps layer (cached separately from app code).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Project layer.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim-bookworm

# ffmpeg for tools/send_audio_message (opus conversion). Static binary copied
# from a single-purpose image avoids ~400MB of apt transitive deps (cairo,
# pango, librsvg, X libs) that ffmpeg's Debian package recommends but we don't
# need for headless audio encoding.
COPY --from=mwader/static-ffmpeg:7.1 /ffmpeg /usr/local/bin/ffmpeg

RUN useradd -r -u 10001 -m -d /home/app app

# Create the volume mountpoints in the IMAGE, owned by app. Docker seeds a
# fresh named volume from the image's directory (contents + ownership) on first
# mount; if the path does not exist in the image, the volume is created
# root-owned instead and this container - which runs as uid 10001 - cannot
# write to it. That is exactly why the audit log silently fell back to
# ephemeral /tmp and was lost on every restart (found 2026-07-16). Doing this
# here means wa-media and wa-audit both come up writable on a clean deploy,
# with no out-of-band chown to remember.
RUN mkdir -p /data/media /var/log/wamcp \
 && chown -R app:app /data/media /var/log/wamcp

WORKDIR /srv
COPY --from=builder --chown=app:app /srv /srv

ENV PATH="/srv/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 9000

# Plain uvicorn (no gunicorn supervisor) - docker already restarts on failure
# and a single async worker handles a personal-scale workload comfortably.
# Explicit --loop uvloop and --http httptools force the C-accelerated paths
# (uvicorn falls back to pure-Python implementations if not requested).
# --proxy-headers / --forwarded-allow-ips lets nginx-set X-Forwarded-* flow.
CMD ["uvicorn", "server:asgi_app", \
     "--host", "0.0.0.0", \
     "--port", "9000", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--timeout-keep-alive", "75", \
     "--limit-concurrency", "50", \
     "--limit-max-requests", "10000", \
     "--backlog", "128", \
     "--no-access-log"]
