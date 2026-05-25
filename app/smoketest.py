"""Synthetic smoke test: load models, run one chunk of silence through the pipeline."""
import os
import time
from pathlib import Path

import numpy as np

from .labels import load_embedded_labels
from .models import GeoFilter, SoundID, Spectrogrammer
from .taxonomy import load_name_map


def run() -> None:
    models_dir = Path(os.environ.get("MODELS_DIR", "/models"))

    print("[smoke] loading…")
    spec = Spectrogrammer(models_dir / "msid685v4_spectrogram.tflite")
    sid = SoundID(models_dir / "sound_id_v49.tflite")
    geo = GeoFilter(models_dir / "geo_v49.tflite")
    sound_labels = load_embedded_labels(models_dir / "sound_id_v49.tflite", "production_labels.txt")
    geo_labels = load_embedded_labels(models_dir / "geo_v49.tflite", "labels.txt")
    name_map = load_name_map(models_dir / "taxonomy.json")
    print(f"[smoke] sound_labels={len(sound_labels)} geo_labels={len(geo_labels)} taxonomy={len(name_map)}")

    audio = np.random.default_rng(0).normal(0, 0.05, 65920).astype(np.float32)

    t0 = time.monotonic()
    image = spec.image(audio)
    t1 = time.monotonic()
    probs = sid.predict(image)
    t2 = time.monotonic()
    priors = geo.priors(35.2, -80.8, 21)
    t3 = time.monotonic()

    print(f"[smoke] spectrogram: {(t1-t0)*1000:.1f}ms  sound_id: {(t2-t1)*1000:.1f}ms  geo: {(t3-t2)*1000:.1f}ms")
    print(f"[smoke] image shape={image.shape} dtype={image.dtype} range=[{image.min():.2f},{image.max():.2f}]")
    print(f"[smoke] probs shape={probs.shape}  bird1={probs[0]:.4f}  max={probs.max():.4f}@{int(probs.argmax())} ({sound_labels[int(probs.argmax())]})")
    print(f"[smoke] priors shape={priors.shape}  nonzero@>=0.0005: {int((priors >= 0.0005).sum())}")

    # Top-10 geo-plausible species for the location
    plausible = [(sound_labels[i], float(probs[i])) for i in range(len(sound_labels))
                 if sound_labels[i] in set(c for c, p in zip(geo_labels, priors) if p >= 0.0005)]
    plausible.sort(key=lambda x: -x[1])
    print(f"[smoke] top-5 plausible species (on noise): {plausible[:5]}")
    print("[smoke] OK")


if __name__ == "__main__":
    run()
