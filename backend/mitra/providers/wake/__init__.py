"""Cloud wake-word detection (PRD FR-2.2). Used only when the on-device spike fails.

Privacy (FR-2.4): detectors see audio in memory only. Nothing here writes audio anywhere.
"""
from __future__ import annotations

import time

import numpy as np

from ..base import ProviderError

OWW_CHUNK_SAMPLES = 1280  # 80 ms, openWakeWord's native frame


class _Refractory:
    def __init__(self, refractory_s: float):
        self.refractory_s = refractory_s
        self._last = 0.0

    def allow(self) -> bool:
        now = time.monotonic()
        if now - self._last < self.refractory_s:
            return False
        self._last = now
        return True


class OpenWakeWordDetector:
    name = "openwakeword"

    def __init__(self, model_ref: str, threshold: float, refractory_s: float):
        from openwakeword.model import Model

        self._model = Model(wakeword_models=[model_ref], inference_framework="onnx")
        self.threshold = threshold
        self._gate = _Refractory(refractory_s)
        self._buf = np.zeros(0, dtype=np.int16)

    def process(self, pcm16: bytes) -> float | None:
        self._buf = np.concatenate([self._buf, np.frombuffer(pcm16, dtype="<i2")])
        hit: float | None = None
        while len(self._buf) >= OWW_CHUNK_SAMPLES:
            chunk, self._buf = self._buf[:OWW_CHUNK_SAMPLES], self._buf[OWW_CHUNK_SAMPLES:]
            scores = self._model.predict(chunk)
            score = max(scores.values()) if scores else 0.0
            if score >= self.threshold and self._gate.allow():
                hit = float(score)
        return hit

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)
        self._model.reset()


class OpenWakeWordFactory:
    name = "openwakeword"

    def __init__(self, model: str, threshold: float = 0.5, refractory_s: float = 2.0):
        try:
            import openwakeword
            from openwakeword.utils import download_models
        except ImportError as e:  # pragma: no cover
            raise ProviderError("openwakeword not installed (pip install '.[wake]')") from e
        self.model = model
        self.threshold = threshold
        self.refractory_s = refractory_s
        if not model.endswith((".onnx", ".tflite")):
            # Stock model name (e.g. hey_jarvis) for pipeline testing until "Hey Smith" is trained.
            download_models(model_names=[model])
        del openwakeword

    def create(self) -> OpenWakeWordDetector:
        return OpenWakeWordDetector(self.model, self.threshold, self.refractory_s)


class MockWakeDetector:
    """Fires on ~300 ms of a loud 1 kHz tone. The device simulator sends that tone as its
    stand-in for "Hey Smith", so the whole cloud-wake path can run without a trained model."""

    name = "mock"

    def __init__(self, refractory_s: float = 1.0):
        self._gate = _Refractory(refractory_s)
        self._buf = np.zeros(0, dtype=np.int16)
        self._hits = 0

    def process(self, pcm16: bytes) -> float | None:
        self._buf = np.concatenate([self._buf, np.frombuffer(pcm16, dtype="<i2")])
        fired: float | None = None
        while len(self._buf) >= OWW_CHUNK_SAMPLES:
            chunk, self._buf = self._buf[:OWW_CHUNK_SAMPLES], self._buf[OWW_CHUNK_SAMPLES:]
            x = chunk.astype(np.float32)
            loud = np.sqrt(np.mean(x * x)) > 3000
            spec = np.abs(np.fft.rfft(x))
            peak_hz = np.argmax(spec) * 16000 / len(x)
            if loud and 900 <= peak_hz <= 1100:
                self._hits += 1
                if self._hits >= 3 and self._gate.allow():
                    fired = 0.99
                    self._hits = 0
            else:
                self._hits = 0
        return fired

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)
        self._hits = 0


class MockWakeFactory:
    name = "mock"

    def create(self) -> MockWakeDetector:
        return MockWakeDetector()
