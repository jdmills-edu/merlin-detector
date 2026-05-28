"""BirdNet-Go API shim backed by the Merlin detector's SQLite.

Surfaces the endpoints BirdNetDisplay hits:
  GET /api/v2/detections?numResults=N
  GET /api/v2/analytics/species/daily
  GET /api/v2/analytics/species
  GET /api/v2/audio/{id}   (currently 404 — no clips captured yet)
  GET /api/v2/events       (SSE; emits each new detection as it lands)
"""
import asyncio
import datetime as dt
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


DB_PATH = Path(os.environ.get("DETECTIONS_DB", "/data/detections.sqlite"))
SOURCE_NAME = os.environ.get("SOURCE_DISPLAY_NAME", "Tapo")
STATIC_DIR = Path(__file__).parent / "static"
EVENT_POLL_SECONDS = float(os.environ.get("EVENT_POLL_SECONDS", "1.0"))
LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "America/New_York"))


def _utc_to_local_clock(ts_utc: str | None) -> str | None:
    """Parse a 'YYYY-MM-DD HH:MM:SS' or ISO UTC timestamp and return a
    leading-HH:MM:SS local-tz string the dashboard's fmtClock regex
    (^(\\d{1,2}):(\\d{2})) can read."""
    if not ts_utc:
        return ts_utc
    s = ts_utc.replace("T", " ").rstrip("Z")
    try:
        naive = dt.datetime.fromisoformat(s[:19])
    except ValueError:
        return ts_utc
    local = naive.replace(tzinfo=dt.timezone.utc).astimezone(LOCAL_TZ)
    return local.strftime("%H:%M:%S")

app = FastAPI(title="merlin-detector → BirdNet-Go shim")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _now_local_date() -> str:
    return dt.datetime.now(LOCAL_TZ).date().isoformat()


def _local_year() -> int:
    return dt.datetime.now(LOCAL_TZ).year


def _season_bounds(today: dt.date) -> tuple[dt.date, dt.date]:
    """Meteorological seasons: Mar/Jun/Sep/Dec starts."""
    y, m = today.year, today.month
    if m in (12, 1, 2):
        start = dt.date(y if m == 12 else y - 1, 12, 1)
        end = dt.date(y if m != 12 else y + 1, 3, 1) - dt.timedelta(days=1)
    elif m in (3, 4, 5):
        start, end = dt.date(y, 3, 1), dt.date(y, 6, 1) - dt.timedelta(days=1)
    elif m in (6, 7, 8):
        start, end = dt.date(y, 6, 1), dt.date(y, 9, 1) - dt.timedelta(days=1)
    else:
        start, end = dt.date(y, 9, 1), dt.date(y, 12, 1) - dt.timedelta(days=1)
    return start, end


def _source_block() -> dict:
    return {"displayName": SOURCE_NAME, "name": SOURCE_NAME.lower().replace(" ", "_"), "id": "merlin-1"}


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "bird-dashboard.html")


@app.get("/favicon.ico")
def favicon() -> RedirectResponse:
    return RedirectResponse("/static/favicon.svg", status_code=301)


@app.get("/healthz")
def healthz() -> dict:
    if not DB_PATH.exists():
        raise HTTPException(503, detail=f"db missing at {DB_PATH}")
    with db() as conn:
        n = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
    return {"ok": True, "detections": n}


def _detection_payload(row: sqlite3.Row) -> dict:
    mic = row["mic_name"] if "mic_name" in row.keys() else None
    return {
        "id": row["id"],
        "date": row["ts_utc"],
        "common_name": row["common_name"] or row["species_code"],
        "commonName": row["common_name"] or row["species_code"],
        "scientific_name": row["scientific_name"] or "",
        "scientificName": row["scientific_name"] or "",
        "confidence": row["confidence"],
        "mic_name": mic,
        "micName": mic,
        "source": _source_block(),
    }


@app.get("/api/v2/detections")
def detections(numResults: int = Query(600, ge=1, le=5000)) -> JSONResponse:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, ts_utc, species_code, common_name, scientific_name, "
            "confidence, mic_name "
            "FROM detections ORDER BY id DESC LIMIT ?",
            (numResults,),
        ).fetchall()
    return JSONResponse([_detection_payload(r) for r in rows])


async def _event_stream():
    """Tail the detections table and emit each new row as an SSE message.

    Polls MAX(id) at EVENT_POLL_SECONDS cadence (default 1s). Cheap because
    the detector writes ≪1 row/min and SQLite reads are sub-millisecond.
    """
    with db() as conn:
        last_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM detections"
        ).fetchone()[0]
    yield f": connected last_id={last_id}\n\n"

    ticks_since_keepalive = 0
    while True:
        await asyncio.sleep(EVENT_POLL_SECONDS)
        try:
            with db() as conn:
                rows = conn.execute(
                    "SELECT id, ts_utc, species_code, common_name, scientific_name, "
                    "confidence, mic_name "
                    "FROM detections WHERE id > ? ORDER BY id ASC LIMIT 100",
                    (last_id,),
                ).fetchall()
        except sqlite3.Error:
            continue  # transient lock or busy — try again next tick
        for r in rows:
            yield f"event: detection\ndata: {json.dumps(_detection_payload(r))}\n\n"
            last_id = r["id"]
        ticks_since_keepalive += 1
        if ticks_since_keepalive >= 15:
            yield ": keepalive\n\n"
            ticks_since_keepalive = 0


@app.get("/api/v2/events")
async def events() -> StreamingResponse:
    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _species_daily(today: str | None = None) -> list[dict]:
    today = today or _now_local_date()
    year = _local_year()
    season_start, season_end = _season_bounds(dt.date.fromisoformat(today))

    with db() as conn:
        rows = conn.execute(
            """
            SELECT species_code, common_name, scientific_name,
                   COUNT(*) AS cnt,
                   MAX(confidence) AS high_conf,
                   MIN(ts_utc) AS first_today,
                   MAX(ts_utc) AS latest_today
            FROM detections
            WHERE date(ts_utc, 'localtime') = ?
            GROUP BY species_code
            ORDER BY cnt DESC
            """,
            (today,),
        ).fetchall()

        out = []
        for r in rows:
            code = r["species_code"]
            hourly = [0] * 24
            for h_row in conn.execute(
                "SELECT CAST(strftime('%H', ts_utc, 'localtime') AS INTEGER) AS h, "
                "COUNT(*) AS c "
                "FROM detections WHERE species_code = ? "
                "AND date(ts_utc, 'localtime') = ? "
                "GROUP BY h",
                (code, today),
            ):
                if 0 <= h_row["h"] < 24:
                    hourly[h_row["h"]] = h_row["c"]

            first_ever_local = conn.execute(
                "SELECT date(MIN(ts_utc), 'localtime') "
                "FROM detections WHERE species_code = ?",
                (code,),
            ).fetchone()[0]
            first_today_local = today
            is_new_this_year = first_ever_local and first_ever_local >= f"{year}-01-01" \
                and first_ever_local == first_today_local
            is_new_this_season = first_ever_local and first_ever_local >= season_start.isoformat() \
                and first_ever_local <= season_end.isoformat() \
                and first_ever_local == first_today_local

            out.append({
                "common_name": r["common_name"] or code,
                "commonName": r["common_name"] or code,
                "scientific_name": r["scientific_name"] or "",
                "scientificName": r["scientific_name"] or "",
                "count": r["cnt"],
                "hourly_counts": hourly,
                "high_confidence": r["high_conf"],
                "first_heard": _utc_to_local_clock(r["first_today"]),
                "firstHeard": _utc_to_local_clock(r["first_today"]),
                "latest_heard": _utc_to_local_clock(r["latest_today"]),
                "latestHeard": _utc_to_local_clock(r["latest_today"]),
                "is_new_this_year": bool(is_new_this_year),
                "isNewThisYear": bool(is_new_this_year),
                "is_new_this_season": bool(is_new_this_season),
                "isNewThisSeason": bool(is_new_this_season),
                "thumbnail_url": None,
                "thumbnailUrl": None,
            })
    return out


@app.get("/api/v2/analytics/species/daily")
def species_daily() -> JSONResponse:
    return JSONResponse(_species_daily())


@app.get("/api/v2/analytics/species")
def species() -> JSONResponse:
    return JSONResponse(_species_daily())


@app.get("/api/v2/audio/{detection_id}")
def audio(detection_id: int):
    # Clip capture not implemented yet — dashboard will gracefully skip audio.
    raise HTTPException(404, detail="audio clips not available yet")


# ---- Bird-call lookup (xeno-canto proxy) ----
# No local clips yet, so the "play call" buttons in the dashboard call this
# endpoint, which proxies xeno-canto's v3 search API and returns the URL of
# a reference recording. The proxy lives here (rather than in JS) so we can
# (a) keep the XC API key off the client, (b) sidestep any future CORS
# changes, and (c) cache lookups in-process — XC rate-limits and a single
# species is only fetched once per shim lifetime.
#
# xeno-canto retired the unauthenticated v2 API; v3 requires a per-account
# key. Register at https://xeno-canto.org/account and set XENO_CANTO_KEY.
_CALL_CACHE: dict[str, dict | None] = {}
_SCI_RE = re.compile(r"^[A-Za-z][A-Za-z\s\-']{1,80}$")
_CALL_LOOKUP_TIMEOUT = float(os.environ.get("CALL_LOOKUP_TIMEOUT", "8"))
_XC_KEY = os.environ.get("XENO_CANTO_KEY", "").strip()


def _xc_query(scientific: str) -> str:
    """Build a v3 `query` value from a Latin binomial. v3 wants
    `gen:Genus+sp:species` rather than the free-text v2 syntax; if the
    caller passed only a single token, fall back to a genus-only query."""
    parts = scientific.strip().split()
    if len(parts) >= 2:
        return f"gen:{parts[0]} sp:{parts[1]} len:2-25"
    return f"gen:{parts[0]} len:2-25"


@app.get("/api/v2/call")
def call_lookup(scientific: str = Query(..., min_length=3, max_length=80)) -> JSONResponse:
    if not _XC_KEY:
        raise HTTPException(
            503,
            detail="XENO_CANTO_KEY not configured on the server "
                   "(register at https://xeno-canto.org/account)",
        )
    key = scientific.strip().lower()
    if not _SCI_RE.match(scientific.strip()):
        raise HTTPException(400, detail="invalid scientific name")
    if key in _CALL_CACHE:
        cached = _CALL_CACHE[key]
        if cached is None:
            raise HTTPException(404, detail="no recordings found")
        return JSONResponse(cached)

    params = urllib.parse.urlencode({
        "query": _xc_query(scientific),
        "per_page": "10",
        "key": _XC_KEY,
    })
    api_url = f"https://xeno-canto.org/api/3/recordings?{params}"
    try:
        with urllib.request.urlopen(api_url, timeout=_CALL_LOOKUP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            raise HTTPException(502, detail="xeno-canto rejected the API key (check XENO_CANTO_KEY)")
        raise HTTPException(502, detail=f"xeno-canto lookup failed: HTTP {e.code}")
    except Exception as e:
        raise HTTPException(502, detail=f"xeno-canto lookup failed: {e}")

    rec = next((r for r in (data.get("recordings") or []) if r.get("file")), None)
    if not rec:
        _CALL_CACHE[key] = None
        raise HTTPException(404, detail="no recordings found")

    file_url = rec["file"]
    if file_url.startswith("//"):
        file_url = "https:" + file_url
    result = {
        "url": file_url,
        "recordist": rec.get("rec") or "",
        "license": rec.get("lic") or "",
        "id": rec.get("id") or "",
        "page": f"https://xeno-canto.org/{rec.get('id')}" if rec.get("id") else "",
        "source": "xeno-canto",
    }
    _CALL_CACHE[key] = result
    return JSONResponse(result)
