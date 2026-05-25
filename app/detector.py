import datetime as dt
import os
import threading
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from .audio import sliding_chunks
from .db import DB
from .labels import load_embedded_labels
from .models import GeoFilter, SoundID, Spectrogrammer
from .taxonomy import load_name_map, name_for


def env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def load_streams() -> list[tuple[str, str]]:
    """Read RTSP_URL / RTSP_URL_2 / ... (and matching MIC_NAME_N) from env.

    Returns a list of (mic_name, url) pairs in ordinal order. Index 1 uses
    the bare RTSP_URL / MIC_NAME for backward compat; 2+ uses the indexed
    form. Stops at the first missing index."""
    streams: list[tuple[str, str]] = []
    primary = os.environ.get("RTSP_URL")
    if primary:
        streams.append((os.environ.get("MIC_NAME", "Mic 1"), primary))
    i = 2
    while True:
        url = os.environ.get(f"RTSP_URL_{i}")
        if not url:
            break
        streams.append((os.environ.get(f"MIC_NAME_{i}", f"Mic {i}"), url))
        i += 1
    if not streams:
        raise RuntimeError("no RTSP streams configured (set RTSP_URL)")
    return streams


def build_geo_mask(
    geo: GeoFilter,
    sound_labels: list[str],
    geo_labels: list[str],
    latitude: float,
    longitude: float,
    geo_threshold: float,
) -> np.ndarray:
    """Boolean mask of length len(sound_labels). True for species the geo model
    considers plausible at this location/week. `bird1` (index 0) is always kept."""
    week = dt.datetime.now(dt.UTC).isocalendar().week
    priors = geo.priors(latitude, longitude, week)
    geo_idx = {code: i for i, code in enumerate(geo_labels)}
    mask = np.zeros(len(sound_labels), dtype=bool)
    mask[0] = True  # bird1 meta-class is location-independent
    kept = 0
    for i, code in enumerate(sound_labels):
        gi = geo_idx.get(code)
        if gi is not None and priors[gi] >= geo_threshold:
            mask[i] = True
            kept += 1
    print(f"[geo] week={week} lat={latitude} lon={longitude} "
          f"kept {kept}/{len(sound_labels) - 1} species "
          f"(threshold={geo_threshold})", flush=True)
    return mask


class SharedState:
    """Mutable cross-thread state. Reassigning `geo_mask` is atomic in
    CPython; workers re-read the attribute each iteration."""
    def __init__(self, geo_mask: np.ndarray):
        self.geo_mask = geo_mask


def stream_worker(
    mic_name: str,
    rtsp_url: str,
    overlap_seconds: float,
    bird_gate: float,
    initial_threshold: float,
    unlocked_threshold: float,
    unlock_hits: int,
    spec_model: Spectrogrammer,
    sound_model: SoundID,
    sound_labels: list[str],
    name_map: dict,
    shared: SharedState,
    inference_lock: threading.Lock,
    db: DB,
) -> None:
    consec_above_initial: dict[int, int] = defaultdict(int)
    heartbeat_at = time.monotonic() + 30
    window_count = 0
    max_gate_since_beat = 0.0

    print(f"[{mic_name}] starting rtsp={rtsp_url}", flush=True)

    for audio in sliding_chunks(rtsp_url, overlap_seconds):
        window_count += 1
        with inference_lock:
            image = spec_model.image(audio)
            probs = sound_model.predict(image)

        gate = float(probs[0])
        max_gate_since_beat = max(max_gate_since_beat, gate)
        if time.monotonic() > heartbeat_at:
            rms = float(np.sqrt(np.mean(audio**2)))
            print(f"[heartbeat:{mic_name}] windows={window_count} "
                  f"max_bird1={max_gate_since_beat:.3f} audio_rms={rms:.4f}",
                  flush=True)
            heartbeat_at = time.monotonic() + 30
            max_gate_since_beat = 0.0

        if gate < bird_gate:
            consec_above_initial.clear()
            continue

        geo_mask = shared.geo_mask
        masked = np.where(geo_mask, probs, 0.0)
        masked[0] = 0.0

        now_iso = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        new_history: dict[int, int] = defaultdict(int)

        candidate_idx = np.where(masked >= unlocked_threshold)[0]
        for idx in candidate_idx:
            p = float(masked[idx])
            unlocked = consec_above_initial.get(idx, 0) >= unlock_hits
            threshold = unlocked_threshold if unlocked else initial_threshold

            if p >= initial_threshold:
                new_history[idx] = consec_above_initial.get(idx, 0) + 1

            if p < threshold:
                continue

            code = sound_labels[idx]
            common, scientific = name_for(code, name_map)
            db.insert(now_iso, code, common, scientific, p, gate, unlocked, mic_name)
            print(f"[detect:{mic_name}] {now_iso}  {common:30s}  p={p:.3f}  "
                  f"gate={gate:.3f}  {'UNLOCKED' if unlocked else 'initial'}  geo_ok=yes",
                  flush=True)

        consec_above_initial = new_history


def main() -> None:
    models_dir = Path(os.environ.get("MODELS_DIR", "/models"))
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))

    streams = load_streams()
    latitude = env_float("LATITUDE", 0.0)
    longitude = env_float("LONGITUDE", 0.0)
    bird_gate = env_float("BIRD_GATE_THRESHOLD", 0.96)
    initial_threshold = env_float("INITIAL_SPECIES_THRESHOLD", 0.50)
    unlocked_threshold = env_float("UNLOCKED_SPECIES_THRESHOLD", 0.20)
    unlock_hits = env_int("UNLOCK_HITS_REQUIRED", 2)
    geo_threshold = env_float("GEO_THRESHOLD", 0.0005)
    overlap_seconds = env_float("OVERLAP_SECONDS", 1.5)

    print("[init] loading models…", flush=True)
    spec_model = Spectrogrammer(models_dir / "msid685v4_spectrogram.tflite")
    sound_model = SoundID(models_dir / "sound_id_v49.tflite")
    geo_model = GeoFilter(models_dir / "geo_v49.tflite")

    sound_labels = load_embedded_labels(models_dir / "sound_id_v49.tflite",
                                        "production_labels.txt")
    geo_labels = load_embedded_labels(models_dir / "geo_v49.tflite", "labels.txt")
    name_map = load_name_map(models_dir / "taxonomy.json")
    print(f"[init] sound labels: {len(sound_labels)}  geo labels: {len(geo_labels)}  "
          f"taxonomy: {len(name_map)}", flush=True)

    geo_mask = build_geo_mask(geo_model, sound_labels, geo_labels,
                              latitude, longitude, geo_threshold)
    shared = SharedState(geo_mask)
    inference_lock = threading.Lock()

    db = DB(data_dir / "detections.sqlite")

    print(f"[run] streams={[name for name, _ in streams]}  "
          f"overlap={overlap_seconds}s  gate={bird_gate} "
          f"initial={initial_threshold} unlocked={unlocked_threshold}",
          flush=True)

    workers: list[threading.Thread] = []
    for mic_name, url in streams:
        t = threading.Thread(
            target=stream_worker,
            name=f"stream-{mic_name}",
            args=(mic_name, url, overlap_seconds, bird_gate,
                  initial_threshold, unlocked_threshold, unlock_hits,
                  spec_model, sound_model, sound_labels, name_map,
                  shared, inference_lock, db),
            daemon=True,
        )
        t.start()
        workers.append(t)

    geo_recompute_at = time.monotonic() + 86400
    while True:
        time.sleep(60)
        for t in workers:
            if not t.is_alive():
                raise RuntimeError(f"stream worker {t.name} died")
        if time.monotonic() > geo_recompute_at:
            shared.geo_mask = build_geo_mask(geo_model, sound_labels, geo_labels,
                                             latitude, longitude, geo_threshold)
            geo_recompute_at = time.monotonic() + 86400


if __name__ == "__main__":
    main()
