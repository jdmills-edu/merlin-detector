FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/birdnet-display

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fetch the dashboard HTML at build time and rewrite BIRDNET_GO_URL so it
# targets whichever host the browser used to load the page (same-origin → our shim).
RUN mkdir -p static && \
    curl -fsSL https://raw.githubusercontent.com/bpm323git/BirdNetDisplay/main/bird-dashboard.html \
      -o static/bird-dashboard.html && \
    sed -i 's|BIRDNET_GO_URL: ""|BIRDNET_GO_URL: window.location.origin|' static/bird-dashboard.html && \
    sed -i 's#if(!Array.isArray(list)||!list.length) continue;#if(!Array.isArray(list)) continue; if(!list.length){ buildPool([]); return true; }#' static/bird-dashboard.html && \
    sed -i 's#</head>#<style>.quiet.on{position:fixed;inset:0;z-index:3;}</style><link rel="stylesheet" href="/static/live-mode.css"><link rel="icon" type="image/svg+xml" href="/static/favicon.svg"></head>#' static/bird-dashboard.html && \
    sed -i 's#</body>#<script src="/static/live-mode.js"></script></body>#' static/bird-dashboard.html

COPY shim.py .
COPY static/live-mode.js static/live-mode.css static/favicon.svg ./static/

ENV PYTHONUNBUFFERED=1 \
    DETECTIONS_DB=/data/detections.sqlite \
    SOURCE_DISPLAY_NAME="Tapo Backyard" \
    TZ=America/New_York

EXPOSE 8080

CMD ["uvicorn", "shim:app", "--host", "0.0.0.0", "--port", "8080"]
