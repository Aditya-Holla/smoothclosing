# SmoothClosing dashboard — production image for Render.
#
# Includes Playwright + Chromium (skipgenie, buyer_tracer, county_downloader)
# and tesseract + poppler (main.py OCR fallback for scanned PDFs).

# Pin to the official Playwright image so we get a tested Chrome install.
# Image is ~1.4GB but cold-start is reasonable on Render Standard.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

# System packages: tesseract for OCR, poppler-utils for pdf2image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install python deps first (better layer caching).
COPY requirements.txt ./
RUN pip install -r requirements.txt

# App code.
COPY . .

# Persistent data volume mount-point. Render persistent disk attaches here.
RUN mkdir -p /data
VOLUME ["/data"]

# Streamlit config: bind 0.0.0.0, no telemetry, no CORS-XSRF gating
# (we put a real auth layer — Cloudflare Access — in front of it).
ENV STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    STREAMLIT_SERVER_ENABLE_CORS=false \
    STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=false

EXPOSE 8501

# Render injects $PORT — fall back to 8501 for local docker run.
# NOTE: GBP posting runs in GitHub Actions (see .github/workflows/gbp-campaign.yml),
# NOT here — this service only serves the dashboard (and shows campaign status).
CMD streamlit run dashboard.py \
    --server.address=0.0.0.0 \
    --server.port=${PORT:-8501}
