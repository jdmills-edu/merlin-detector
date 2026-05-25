# merlin-detector

Self-hosted bird detector: RTSP audio → Merlin TFLite models → SQLite, with a
journal/grid dashboard and an MCP server for local LLMs.

Three containers, one shared SQLite volume:

```
  Tapo/RTSP camera
        │  audio
        ▼
  ┌──────────────┐                 ┌──────────────────┐
  │  detector    │── writes ──▶    │  ./data/         │
  │  (ffmpeg +   │                 │  detections.sqlite│
  │  TFLite)     │                 └──────────────────┘
  └──────────────┘                      ▲         ▲
                                        │ ro      │ ro
                                  ┌─────┴─────┐ ┌─┴────────┐
                                  │  display  │ │   mcp    │
                                  │ (FastAPI  │ │ (FastMCP │
                                  │  + SSE)   │ │  HTTP)   │
                                  └───────────┘ └──────────┘
                                    :8090         :8091/mcp
```

The detector chunks RTSP audio into ~3 s windows, runs the Merlin sound-ID
model, gates with the geo prior + a generic "is this a bird?" head, then
appends qualifying detections to SQLite. The display container serves a
patched copy of [BirdNetDisplay] backed by a small BirdNet-Go-shaped shim,
plus a live 2×4 grid mode driven by SSE. The MCP container exposes five
read-only query tools for local LLMs to call.

[BirdNetDisplay]: https://github.com/bpm323git/BirdNetDisplay

## Prerequisites

- Docker + Docker Compose
- An RTSP audio source (Tapo C-series, ONVIF cam, anything ffmpeg can open)
- Your latitude/longitude (decimal degrees)
- The Merlin TFLite model bundle (see below — not redistributed here)

### Models

`./models/` must contain the Merlin app's TFLite bundle. These are not
included in this repo — they're Cornell Lab IP extracted from the Merlin
Android/iOS app. Expected files:

```
models/
  model_v55.tflite              # sound ID
  sound_id_v49.tflite           # alternate sound ID
  geo_v49.tflite                # geographic prior
  msid685v4_spectrogram.tflite  # mel spectrogram frontend
  ui_spectrogram.tflite
  species_manifest.json
  sound_id_manifest.json
  idtext.json
  taxonomy.json
```

Source these yourself from the Merlin app bundle. The directory is mounted
read-only into the detector container at `/models`.

## Setup

```bash
cp .env.example .env
# edit .env: RTSP_URL, LATITUDE, LONGITUDE
mkdir -p data clips
# drop your model files into ./models/
docker compose up -d --build
```

First boot will fetch the dashboard HTML from the BirdNetDisplay repo,
install Python deps in each image, and start ffmpeg pulling from your RTSP
URL. Detections start landing in `./data/detections.sqlite` immediately.

## Endpoints

| Service  | Default port | Notes                                          |
|----------|--------------|------------------------------------------------|
| detector | none         | Writes SQLite only                             |
| display  | `:8090`      | `/` dashboard, `/healthz`, `/api/v2/*`         |
| mcp      | `:8091`      | `/mcp` streamable-HTTP MCP endpoint            |

Override with `DISPLAY_PORT` / `MCP_PORT` in `.env`.

### Verify

```bash
curl localhost:8090/healthz                  # {"ok":true,"detections":N}
curl localhost:8090/api/v2/detections?numResults=5
open http://localhost:8090                   # journal — click status text for grid
```

## MCP client config

The MCP server speaks [Streamable HTTP][mcp-transport] on `/mcp`.

[mcp-transport]: https://modelcontextprotocol.io/specification/draft/basic/transports

Tools registered:

- `list_species_today` — counts + first/last heard for today
- `list_recent_detections(limit=25)` — newest-first detection rows
- `species_history(name)` — substring match, all-time totals + today's hourly histogram
- `summary_stats(days=1)` — totals, distinct species, busiest local hour, top 5
- `search_species(query, days=7)` — "any wrens this week?" style lookup

Claude Desktop / LM Studio / OpenWebUI:

```json
{
  "mcpServers": {
    "merlin-detector": {
      "transport": "streamable-http",
      "url": "http://<docker-host>:8091/mcp"
    }
  }
}
```

## Tuning

All in `.env` (see `.env.example` for current defaults):

| Var | Default | What it does |
|---|---|---|
| `BIRD_GATE_THRESHOLD` | `0.96` | Generic bird-vs-not gate. Lower → more detections, more noise. |
| `INITIAL_SPECIES_THRESHOLD` | `0.50` | Confidence to register a species' first hit. |
| `UNLOCKED_SPECIES_THRESHOLD` | `0.20` | Confidence after a species has been "unlocked". |
| `UNLOCK_HITS_REQUIRED` | `2` | How many initial-threshold hits before a species unlocks. |
| `GEO_THRESHOLD` | `0.0005` | Geographic prior cutoff for plausibility. |
| `OVERLAP_SECONDS` | `1.5` | Sliding-window overlap for audio chunks. |
| `TZ` | `America/New_York` | Container timezone — affects local-time queries everywhere. |

## Layout

```
app/                   # detector: audio pipeline + TFLite inference + SQLite writer
display/               # FastAPI shim + patched BirdNetDisplay + live grid overlay
  shim.py              # BirdNet-Go-shaped endpoints + SSE
  static/live-mode.*   # 2x4 grid mode, status-dot animation, fade-out
  Dockerfile           # fetches and patches bird-dashboard.html at build time
mcp/                   # FastMCP streamable-HTTP server, read-only over SQLite
docker-compose.yml
.env.example
```

## License

MIT for code in this repo. The Merlin model files in `./models/` are not
covered by this license — see Cornell Lab terms for the Merlin app.
The fetched `bird-dashboard.html` is MIT, from
[bpm323git/BirdNetDisplay](https://github.com/bpm323git/BirdNetDisplay).
