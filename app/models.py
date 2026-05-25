import numpy as np
from pathlib import Path
from ai_edge_litert.interpreter import Interpreter


SPEC_FRAME_SAMPLES = 512   # spectrogram model input length
MEL_BINS = 128              # spectrogram column height
TIME_COLUMNS = 512          # sound_id input width
# hop is chosen so 512 columns span 65920 audio samples:
#   65920 = (TIME_COLUMNS - 1) * HOP + SPEC_FRAME_SAMPLES  →  HOP = 128
SPEC_HOP = (65920 - SPEC_FRAME_SAMPLES) // (TIME_COLUMNS - 1)


class Spectrogrammer:
    def __init__(self, path: Path):
        self.interp = Interpreter(model_path=str(path))
        self.interp.allocate_tensors()
        self.in_idx = self.interp.get_input_details()[0]["index"]
        self.out_idx = self.interp.get_output_details()[0]["index"]

    def column(self, frame: np.ndarray) -> np.ndarray:
        # frame: float32[512] → uint8[128, 1]
        self.interp.set_tensor(self.in_idx, frame.astype(np.float32))
        self.interp.invoke()
        return self.interp.get_tensor(self.out_idx)

    def image(self, audio: np.ndarray) -> np.ndarray:
        """audio: float32[65920] → float32[1, 128, 512, 3] normalized to [-1, 1]."""
        assert audio.shape == (65920,), audio.shape
        cols = np.empty((MEL_BINS, TIME_COLUMNS), dtype=np.uint8)
        for i in range(TIME_COLUMNS):
            start = i * SPEC_HOP
            frame = audio[start:start + SPEC_FRAME_SAMPLES]
            cols[:, i] = self.column(frame)[:, 0]
        # normalize uint8 [0,255] → float32 [-1, 1]
        norm = (cols.astype(np.float32) / 127.5) - 1.0
        # replicate to 3 channels
        rgb = np.repeat(norm[:, :, None], 3, axis=2)
        return rgb[None, ...]  # [1, 128, 512, 3]


class SoundID:
    def __init__(self, path: Path):
        self.interp = Interpreter(model_path=str(path))
        self.interp.allocate_tensors()
        self.in_idx = self.interp.get_input_details()[0]["index"]
        self.out_idx = self.interp.get_output_details()[0]["index"]

    def predict(self, image: np.ndarray) -> np.ndarray:
        self.interp.set_tensor(self.in_idx, image.astype(np.float32))
        self.interp.invoke()
        return self.interp.get_tensor(self.out_idx)[0]  # [2301]


class GeoFilter:
    def __init__(self, path: Path):
        self.interp = Interpreter(model_path=str(path))
        self.interp.allocate_tensors()
        self.in_map = {d["name"]: d["index"] for d in self.interp.get_input_details()}
        self.out_idx = self.interp.get_output_details()[0]["index"]

    def priors(self, latitude: float, longitude: float, week_of_year: int) -> np.ndarray:
        self.interp.set_tensor(self.in_map["serving_default_latitude:0"],
                               np.array([latitude], dtype=np.float32))
        self.interp.set_tensor(self.in_map["serving_default_longitude:0"],
                               np.array([longitude], dtype=np.float32))
        self.interp.set_tensor(self.in_map["serving_default_week_of_year:0"],
                               np.array([float(week_of_year)], dtype=np.float32))
        self.interp.invoke()
        return self.interp.get_tensor(self.out_idx)[0]  # [3017]
