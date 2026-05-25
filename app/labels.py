import zipfile
from pathlib import Path


def load_embedded_labels(tflite_path: Path, name: str) -> list[str]:
    with zipfile.ZipFile(tflite_path) as z:
        return z.read(name).decode("utf-8").splitlines()
