import sqlite3
from pathlib import Path
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    species_code TEXT NOT NULL,
    common_name TEXT,
    scientific_name TEXT,
    confidence REAL NOT NULL,
    bird_gate REAL NOT NULL,
    unlocked INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_detections_ts ON detections(ts_utc);
CREATE INDEX IF NOT EXISTS idx_detections_species ON detections(species_code);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def insert(self, ts_utc, species_code, common, scientific, confidence, bird_gate, unlocked):
        self.conn.execute(
            "INSERT INTO detections(ts_utc, species_code, common_name, scientific_name, confidence, bird_gate, unlocked) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts_utc, species_code, common, scientific, confidence, bird_gate, int(unlocked)),
        )
        self.conn.commit()
