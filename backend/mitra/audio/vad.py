"""Server-side end-of-utterance detection.

The device already gates silence with its own VAD; this detector decides *when the user has
finished talking* so the turn can move to processing. Energy-based with an adaptive noise floor:
no native dependency, predictable on a small EC2 instance.
"""
from __future__ import annotations

from dataclasses import dataclass

from .pcm import bytes_to_ms, rms_dbfs

FRAME_MS = 20
FRAME_BYTES = 640  # 20 ms @ 16 kHz pcm16


@dataclass
class EndpointEvent:
    kind: str  # "speech_start" | "speech_end"
    at_ms: float


class EndpointDetector:
    def __init__(
        self,
        end_silence_ms: int = 800,
        min_speech_ms: int = 200,
        margin_db: float = 12.0,
        floor_db: float = -52.0,
    ):
        self.end_silence_ms = end_silence_ms
        self.min_speech_ms = min_speech_ms
        self.margin_db = margin_db
        self.floor_db = floor_db
        self.noise_db = -60.0
        self.reset()

    def reset(self) -> None:
        self._buf = b""
        self.t_ms = 0.0
        self.in_speech = False
        self.speech_ms = 0.0
        self.silence_ms = 0.0
        self.heard_speech = False

    @property
    def threshold_db(self) -> float:
        return max(self.noise_db + self.margin_db, self.floor_db)

    def feed(self, pcm16: bytes) -> list[EndpointEvent]:
        events: list[EndpointEvent] = []
        self._buf += pcm16
        while len(self._buf) >= FRAME_BYTES:
            frame, self._buf = self._buf[:FRAME_BYTES], self._buf[FRAME_BYTES:]
            self.t_ms += bytes_to_ms(FRAME_BYTES)
            level = rms_dbfs(frame)
            voiced = level > self.threshold_db
            if not voiced and not self.in_speech:
                # Track background noise only while nobody is talking.
                self.noise_db = 0.95 * self.noise_db + 0.05 * level
            if voiced:
                self.silence_ms = 0.0
                self.speech_ms += FRAME_MS
                if not self.in_speech and self.speech_ms >= self.min_speech_ms:
                    self.in_speech = True
                    self.heard_speech = True
                    events.append(EndpointEvent("speech_start", self.t_ms))
            else:
                if not self.in_speech:
                    self.speech_ms = max(0.0, self.speech_ms - FRAME_MS)
                else:
                    self.silence_ms += FRAME_MS
                    if self.silence_ms >= self.end_silence_ms:
                        self.in_speech = False
                        self.speech_ms = 0.0
                        events.append(EndpointEvent("speech_end", self.t_ms))
        return events
