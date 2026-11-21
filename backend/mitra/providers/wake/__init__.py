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
        self.best = 0.0   # highest score since the caller last reset it (diagnostics only)

    def process(self, pcm16: bytes) -> float | None:
        self._buf = np.concatenate([self._buf, np.frombuffer(pcm16, dtype="<i2")])
        hit: float | None = None
        while len(self._buf) >= OWW_CHUNK_SAMPLES:
            chunk, self._buf = self._buf[:OWW_CHUNK_SAMPLES], self._buf[OWW_CHUNK_SAMPLES:]
            scores = self._model.predict(chunk)
            score = max(scores.values()) if scores else 0.0
            self.best = max(self.best, float(score))
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


class SttKeywordDetector:
    """Wake word by transcription: cuts idle uplink into short speech segments; the session
    transcribes each with the configured STT and wakes when a keyword (e.g. "smith") is heard.

    No model training needed, any word works, ~1-2 s slower than a trained detector. Audio
    segments live in RAM only and are dropped after transcription (FR-2.4).
    """

    name = "stt"
    needs_stt = True   # the session must call feed()/matches() instead of process()

    def __init__(self, keywords: list[str], margin_db: float = 8.0, end_silence_ms: int = 500,
                 min_speech_ms: int = 160, max_segment_s: float = 4.0, preroll_ms: int = 300):
        import re

        alts = "|".join(re.escape(k.strip().lower()) for k in keywords if k.strip())
        # Whisper spells a lone name loosely ("Smith", "Smith.", "Smyth", "Smit"): allow y/i and
        # an optional final h for words ending in "ith".
        alts = alts.replace("ith", "[iy]th?")
        self._re = re.compile(rf"\b(?:{alts})\b", re.IGNORECASE)
        self.keywords = keywords
        self.margin_db, self.end_silence_ms, self.min_speech_ms = margin_db, end_silence_ms, min_speech_ms
        self.max_bytes = int(max_segment_s * 32000)
        self.preroll_frames = preroll_ms // 20
        self.threshold = None   # for the "wake check" log
        self.best = 0.0
        self.reset()

    def reset(self) -> None:
        self._buf = b""
        self._pre: list[bytes] = []
        self._seg = bytearray()
        self._in_speech = False
        self._voiced_ms = 0
        self._silence_ms = 0
        if not hasattr(self, "noise_db"):
            self.noise_db: float | None = None

    def process(self, pcm16: bytes) -> float | None:   # unused: see needs_stt
        return None

    def feed(self, pcm16: bytes) -> list[bytes]:
        """Returns finished speech segments (pcm16) ready to transcribe."""
        from ...audio.pcm import rms_dbfs

        out: list[bytes] = []
        self._buf += pcm16
        while len(self._buf) >= 640:
            frame, self._buf = self._buf[:640], self._buf[640:]
            level = rms_dbfs(frame)
            if self.noise_db is None:
                self.noise_db = level
            elif level < self.noise_db:
                self.noise_db = 0.7 * self.noise_db + 0.3 * level        # follow quiet fast
            elif not self._in_speech:
                self.noise_db = 0.995 * self.noise_db + 0.005 * level    # creep up slowly
            voiced = level > max(self.noise_db + self.margin_db, -55.0)
            if not self._in_speech:
                self._pre = (self._pre + [frame])[-self.preroll_frames:]
                self._voiced_ms = self._voiced_ms + 20 if voiced else max(0, self._voiced_ms - 20)
                if self._voiced_ms >= self.min_speech_ms:
                    self._in_speech, self._silence_ms = True, 0
                    self._seg = bytearray(b"".join(self._pre))
                continue
            self._seg += frame
            self._silence_ms = 0 if voiced else self._silence_ms + 20
            if self._silence_ms >= self.end_silence_ms or len(self._seg) >= self.max_bytes:
                out.append(bytes(self._seg))
                self._seg, self._pre = bytearray(), []
                self._in_speech, self._voiced_ms = False, 0
        return out

    def matches(self, text: str) -> bool:
        return bool(self._re.search(text or ""))


class SttKeywordFactory:
    name = "stt"

    def __init__(self, keywords: list[str]):
        if not [k for k in keywords if k.strip()]:
            raise ProviderError("wake provider 'stt' needs at least one keyword (MITRA_WAKE_WORDS)")
        self.keywords = keywords

    def create(self) -> SttKeywordDetector:
        return SttKeywordDetector(self.keywords)


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
