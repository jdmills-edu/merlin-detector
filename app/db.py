import sqlite3
import threading
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    species_code TEXT NOT NULL,
    common_name TEXT,
    scientific_name TEXT,
    confidence REAL NOT NULL,
    bird_gate REAL NOT NULL,
    unlocked INTEGER NOT NULL,
    mic_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_detections_ts ON detections(ts_utc);
CREATE INDEX IF NOT EXISTS idx_detections_species ON detections(species_code);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        # Migrate older DBs whose detections table predates mic_name. The
        # column must exist before we can index it.
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(detections)")}
        if "mic_name" not in cols:
            self.conn.execute("ALTER TABLE detections ADD COLUMN mic_name TEXT")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_detections_mic ON detections(mic_name)"
        )
        self.conn.commit()
        self._lock = threading.Lock()

    def insert(self, ts_utc, species_code, common, scientific, confidence,
               bird_gate, unlocked, mic_name):
        with self._lock:
            self.conn.execute(
                "INSERT INTO detections(ts_utc, species_code, common_name, scientific_name, "
                "confidence, bird_gate, unlocked, mic_name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts_utc, species_code, common, scientific, confidence,
                 bird_gate, int(unlocked), mic_name),
            )
            self.conn.commit()
