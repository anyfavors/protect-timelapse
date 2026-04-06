# ── Stage 1: Compile Tailwind CSS ───────────────────────────────────────────
FROM node:22-slim AS css-build

WORKDIR /src
COPY package.json .
RUN npm install --no-fund --no-audit
COPY static/app.css.src ./app.css.src
COPY templates/index.html ./templates/index.html
RUN npx @tailwindcss/cli -i app.css.src -o app.css --minify


# ── Stage 2: Python runtime ──────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

ARG BUILD_DATE
ARG VERSION=dev

LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.title="protect-timelapse" \
      org.opencontainers.image.description="Self-hosted timelapse generator for UniFi Protect cameras" \
      org.opencontainers.image.source="https://github.com/protect-timelapse/protect-timelapse"

# Don't write .pyc files, flush stdout/stderr immediately
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps: ffmpeg for rendering
RUN apt-get update && apt-get install -y --no-install-recommends --no-install-suggests \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (cached layer — only busts when requirements.txt changes)
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

# Copy application code in specific layers for better cache granularity
COPY app/ ./app/
COPY templates/ ./templates/
COPY static/app.js ./static/app.js

# Inject compiled CSS from the CSS build stage
COPY --from=css-build /src/app.css ./static/app.css

# Non-root user for security (B9)
RUN useradd -m -u 1000 appuser && \
    mkdir -p /data && \
    chown appuser:appuser /data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --start-interval=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

# Entrypoint fixes /data ownership (top-level only), then drops to appuser
ENTRYPOINT ["/entrypoint.sh"]
