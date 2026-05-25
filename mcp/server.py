"""MCP server exposing read-only queries over the Merlin detector's SQLite.

Transport: Streamable HTTP on 0.0.0.0:8080 (port-mapped externally in
docker-compose). Tools are designed for a local LLM that wants to answer
ad-hoc questions about what the detector is hearing without learning the
schema.

All timestamps are returned in the container's local timezone (TZ env,
default America/New_York) so model output reads naturally for the user.
"""
from __future__ import annotations

import datetime as dt
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP


DB_PATH = Path(os.environ.get("DETECTIONS_DB", "/data/detections.sqlite"))
LOCAL_TZ = ZoneInfo(os.environ.get("TZ", "America/New_York"))

mcp = FastMCP("merlin-detector")


@contextmanager
def _db():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _today_local() -> str:
    return dt.datetime.now(LOCAL_TZ).date().isoformat()


def _utc_to_local(ts_utc: str | None) -> str | None:
    if not ts_utc:
        return None
    s = ts_utc.replace("T", " ").rstrip("Z")
    try:
        naive = dt.datetime.fromisoformat(s[:19])
    except ValueError:
        return ts_utc
    return naive.replace(tzinfo=dt.timezone.utc).astimezone(LOCAL_TZ).isoformat(sep=" ")


@mcp.tool()
def list_species_today() -> list[dict[str, Any]]:
    """Species detected so far today, with detection counts and first/last
    heard times (local). Ordered by count, descending."""
    today = _today_local()
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT common_name, scientific_name, species_code,
                   COUNT(*) AS count,
                   MIN(ts_utc) AS first_utc,
                   MAX(ts_utc) AS last_utc,
                   MAX(confidence) AS top_confidence
            FROM detections
            WHERE date(ts_utc, 'localtime') = ?
            GROUP BY species_code
            ORDER BY count DESC, last_utc DESC
            """,
            (today,),
        ).fetchall()
    return [
        {
            "common_name": r["common_name"] or r["species_code"],
            "scientific_name": r["scientific_name"] or "",
            "count": r["count"],
            "first_heard": _utc_to_local(r["first_utc"]),
            "last_heard": _utc_to_local(r["last_utc"]),
            "top_confidence": round(r["top_confidence"], 3),
        }
        for r in rows
    ]


@mcp.tool()
def list_recent_detections(limit: int = 25) -> list[dict[str, Any]]:
    """The most recent detections, newest first. Each row is a single
    detection event (a species may appear multiple times)."""
    limit = max(1, min(int(limit), 500))
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT id, ts_utc, common_name, scientific_name, species_code, confidence
            FROM detections
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "when": _utc_to_local(r["ts_utc"]),
            "common_name": r["common_name"] or r["species_code"],
            "scientific_name": r["scientific_name"] or "",
            "confidence": round(r["confidence"], 3),
        }
        for r in rows
    ]


@mcp.tool()
def species_history(name: str) -> dict[str, Any]:
    """All-time history for one species. `name` matches the common or
    scientific name case-insensitively (substring). Returns the best
    match's totals, first/last heard ever, today's count, and a 24-element
    hourly histogram of today's detections (index 0 = midnight local)."""
    q = f"%{name.strip().lower()}%"
    today = _today_local()
    with _db() as conn:
        match = conn.execute(
            """
            SELECT common_name, scientific_name, species_code,
                   COUNT(*) AS total,
                   MIN(ts_utc) AS first_utc,
                   MAX(ts_utc) AS last_utc
            FROM detections
            WHERE LOWER(common_name) LIKE ? OR LOWER(scientific_name) LIKE ?
            GROUP BY species_code
            ORDER BY total DESC
            LIMIT 1
            """,
            (q, q),
        ).fetchone()
        if not match:
            return {"error": f"no detections matching {name!r}"}

        code = match["species_code"]
        today_count = conn.execute(
            "SELECT COUNT(*) FROM detections "
            "WHERE species_code = ? AND date(ts_utc, 'localtime') = ?",
            (code, today),
        ).fetchone()[0]

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

    return {
        "common_name": match["common_name"] or code,
        "scientific_name": match["scientific_name"] or "",
        "total_all_time": match["total"],
        "first_ever": _utc_to_local(match["first_utc"]),
        "last_ever": _utc_to_local(match["last_utc"]),
        "count_today": today_count,
        "hourly_today": hourly,
    }


@mcp.tool()
def summary_stats(days: int = 1) -> dict[str, Any]:
    """Aggregate stats over the last N local days (default 1 = today).
    Returns total detections, distinct species, busiest local hour, and
    the top 5 species in the window."""
    days = max(1, min(int(days), 365))
    today_local = dt.datetime.now(LOCAL_TZ).date()
    start_local = (today_local - dt.timedelta(days=days - 1)).isoformat()

    with _db() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS detections,
                   COUNT(DISTINCT species_code) AS species
            FROM detections
            WHERE date(ts_utc, 'localtime') >= ?
            """,
            (start_local,),
        ).fetchone()

        busiest = conn.execute(
            """
            SELECT CAST(strftime('%H', ts_utc, 'localtime') AS INTEGER) AS h,
                   COUNT(*) AS c
            FROM detections
            WHERE date(ts_utc, 'localtime') >= ?
            GROUP BY h
            ORDER BY c DESC
            LIMIT 1
            """,
            (start_local,),
        ).fetchone()

        top = conn.execute(
            """
            SELECT common_name, scientific_name, species_code, COUNT(*) AS c
            FROM detections
            WHERE date(ts_utc, 'localtime') >= ?
            GROUP BY species_code
            ORDER BY c DESC
            LIMIT 5
            """,
            (start_local,),
        ).fetchall()

    return {
        "window_days": days,
        "window_start_local": start_local,
        "total_detections": totals["detections"],
        "distinct_species": totals["species"],
        "busiest_hour_local": busiest["h"] if busiest else None,
        "busiest_hour_count": busiest["c"] if busiest else 0,
        "top_species": [
            {
                "common_name": r["common_name"] or r["species_code"],
                "scientific_name": r["scientific_name"] or "",
                "count": r["c"],
            }
            for r in top
        ],
    }


@mcp.tool()
def search_species(query: str, days: int = 7) -> list[dict[str, Any]]:
    """Find species matching `query` (substring of common or scientific
    name, case-insensitive) heard within the last N local days. Useful
    for 'have I heard any wrens this week?' style questions."""
    days = max(1, min(int(days), 365))
    q = f"%{query.strip().lower()}%"
    start_local = (dt.datetime.now(LOCAL_TZ).date() - dt.timedelta(days=days - 1)).isoformat()

    with _db() as conn:
        rows = conn.execute(
            """
            SELECT common_name, scientific_name, species_code,
                   COUNT(*) AS count,
                   MAX(ts_utc) AS last_utc
            FROM detections
            WHERE (LOWER(common_name) LIKE ? OR LOWER(scientific_name) LIKE ?)
              AND date(ts_utc, 'localtime') >= ?
            GROUP BY species_code
            ORDER BY last_utc DESC
            """,
            (q, q, start_local),
        ).fetchall()

    return [
        {
            "common_name": r["common_name"] or r["species_code"],
            "scientific_name": r["scientific_name"] or "",
            "count": r["count"],
            "last_heard": _utc_to_local(r["last_utc"]),
        }
        for r in rows
    ]


if __name__ == "__main__":
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8080
    mcp.run(transport="streamable-http")
