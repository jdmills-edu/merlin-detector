import os
import select
import subprocess
import time
from typing import Iterator

import numpy as np


SAMPLE_RATE = 22050
CHUNK_SAMPLES = 65920  # ~2.99s, the sound_id model's audio context window

# kill ffmpeg if no audio bytes arrive for this long
READ_STALL_SECONDS = float(os.environ.get("READ_STALL_SECONDS", "20"))


def open_ffmpeg(rtsp_url: str) -> subprocess.Popen:
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-f", "f32le",
        "pipe:1",
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=None, bufsize=0)


def sliding_chunks(rtsp_url: str, overlap_seconds: float) -> Iterator[np.ndarray]:
    """Yield CHUNK_SAMPLES-long float32 windows with the given overlap, sourced from RTSP."""
    if not (0 <= overlap_seconds < CHUNK_SAMPLES / SAMPLE_RATE):
        raise ValueError(f"overlap_seconds must be in [0, {CHUNK_SAMPLES / SAMPLE_RATE})")
    hop_samples = CHUNK_SAMPLES - int(overlap_seconds * SAMPLE_RATE)
    chunk_bytes = hop_samples * 4

    while True:
        print(f"[audio] starting ffmpeg → {rtsp_url}", flush=True)
        proc = open_ffmpeg(rtsp_url)
        buf = np.empty(0, dtype=np.float32)
        stalled = False
        try:
            while True:
                ready, _, _ = select.select([proc.stdout], [], [], READ_STALL_SECONDS)
                if not ready:
                    print(f"[audio] no audio for {READ_STALL_SECONDS:.0f}s — killing ffmpeg",
                          flush=True)
                    stalled = True
                    break
                raw = proc.stdout.read(chunk_bytes)
                if not raw:
                    break  # pipe closed
                chunk = np.frombuffer(raw, dtype=np.float32)
                buf = np.concatenate([buf, chunk]) if buf.size else chunk
                while buf.size >= CHUNK_SAMPLES:
                    yield buf[:CHUNK_SAMPLES].copy()
                    buf = buf[hop_samples:]
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()
        print(f"[audio] ffmpeg ended (stalled={stalled}), reconnecting in 5s", flush=True)
        time.sleep(5)
