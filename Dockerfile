# ── Stage 1: Compile Tailwind CSS + minify app.js ───────────────────────────
FROM node:25-slim AS css-build

WORKDIR /src

# Copy lockfile first so npm layer is cached independently of source changes (D2/D3).
# Run: `npm install` locally and commit package-lock.json to enable `npm ci`.
# Prefer npm ci for reproducible builds; fall back to npm install if no lockfile (H1)
COPY package.json package-lock.json* ./
RUN --mount=type=cache,target=/root/.npm \
    if [ -f package-lock.json ]; then npm ci --prefer-offline --no-fund --no-audit; \
    else npm install --prefer-offline --no-fund --no-audit; fi

COPY static/app.css.src ./app.css.src
COPY templates/index.html ./templates/index.html
RUN npx @tailwindcss/cli -i app.css.src -o app.css --minify

# Minify app.js — reduces bundle ~35% before gzip (D4)
COPY static/app.js ./app.js
RUN npx terser app.js --compress --mangle --output app.min.js \
    || cp app.js app.min.js  # fall back to unminified if terser unavailable


# ── Stage 2: Python runtime ──────────────────────────────────────────────────
FROM python:3.14-slim-bookworm

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

# System deps: ffmpeg for rendering, tini for PID1 signal handling + zombie reaping, curl for healthcheck
# apt-get upgrade pulls in security patches for base image packages (Trivy scan)
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends --no-install-suggests \
        ffmpeg \
        tini \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps (cached layer — only busts when requirements.txt changes)
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir -r requirements.txt

# Copy application code in specific layers for better cache granularity
COPY app/ ./app/
COPY templates/ ./templates/

# Inject minified JS and compiled CSS from build stage (D4)
COPY --from=css-build /src/app.min.js ./static/app.js
COPY --from=css-build /src/app.css ./static/app.css

# Non-root user for security (B9)
RUN useradd -m -u 1000 appuser && \
    mkdir -p /data /home/appuser/.config/ufp && \
    chown -R appuser:appuser /data /home/appuser/.config/ufp

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Switch to non-root user (H6) — entrypoint runs as appuser directly
USER appuser

EXPOSE 8080

# curl is faster than spawning Python for health checks (D1)
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --start-interval=5s --retries=3 \
    CMD curl -f http://localhost:8080/api/health/live || exit 1

# tini as PID1: forwards signals to uvicorn and reaps zombie ffmpeg processes
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
