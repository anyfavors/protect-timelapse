# ── Stage 1: Compile Tailwind CSS ───────────────────────────────────────────
FROM node:22-slim AS css-build

WORKDIR /src
COPY package.json .
RUN npm install --no-fund --no-audit
COPY static/app.css.src ./app.css.src
COPY templates/index.html ./templates/index.html
RUN npx @tailwindcss/cli -i app.css.src -o app.css --minify


# ── Stage 2: Python runtime ──────────────────────────────────────────────────
FROM python:3.12-slim

ARG BUILD_DATE
ARG VERSION=dev

LABEL org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.title="protect-timelapse"

WORKDIR /app

# System deps: ffmpeg for rendering, curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Inject compiled CSS from the CSS build stage
COPY --from=css-build /src/app.css ./static/app.css

# Non-root user for security (B9)
RUN useradd -m -u 1000 appuser && \
    mkdir -p /data && \
    chown -R appuser:appuser /app /data

USER appuser

# Storage volume — must be mounted at runtime
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/api/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
