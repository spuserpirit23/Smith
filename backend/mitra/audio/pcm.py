"""Small PCM helpers (numpy only, no native deps)."""
from __future__ import annotations

import io
import wave

import numpy as np

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2


def ms_to_bytes(ms: float, rate: int = SAMPLE_RATE) -> int:
    return int(rate * ms / 1000) * BYTES_PER_SAMPLE


def bytes_to_ms(n: int, rate: int = SAMPLE_RATE) -> float:
    return n / BYTES_PER_SAMPLE / rate * 1000


def to_wav(pcm16: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16)
    return buf.getvalue()


def from_wav(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("expected 16-bit WAV")
        frames = w.readframes(w.getnframes())
        rate = w.getframerate()
        if w.getnchannels() == 2:
            arr = np.frombuffer(frames, dtype="<i2").reshape(-1, 2).mean(axis=1).astype("<i2")
            frames = arr.tobytes()
        return frames, rate


def resample(pcm16: bytes, src_rate: int, dst_rate: int = SAMPLE_RATE) -> bytes:
    """Linear-interpolation resampler; adequate for speech going to a 1.5" speaker."""
    if src_rate == dst_rate or not pcm16:
        return pcm16
    x = np.frombuffer(pcm16, dtype="<i2").astype(np.float32)
    n_out = int(round(len(x) * dst_rate / src_rate))
    if n_out <= 0:
        return b""
    t_out = np.linspace(0, len(x) - 1, n_out, dtype=np.float32)
    y = np.interp(t_out, np.arange(len(x), dtype=np.float32), x)
    return np.clip(y, -32768, 32767).astype("<i2").tobytes()


def rms_dbfs(pcm16: bytes) -> float:
    if len(pcm16) < 2:
        return -120.0
    x = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype="<i2").astype(np.float32)
    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
    if rms <= 0:
        return -120.0
    return 20 * np.log10(rms / 32768.0)


def tone(freq_hz: float, ms: int, amplitude: float = 0.3, rate: int = SAMPLE_RATE) -> bytes:
    t = np.arange(int(rate * ms / 1000)) / rate
    env = np.minimum(1, np.minimum(t, t[::-1]) / 0.01)  # 10 ms fade in/out
    y = amplitude * env * np.sin(2 * np.pi * freq_hz * t)
    return (y * 32767).astype("<i2").tobytes()
