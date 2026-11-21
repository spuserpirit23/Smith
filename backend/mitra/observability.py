"""Structured JSON logs + Prometheus metrics (vendor-neutral; scrape with anything, or ship the
container logs to CloudWatch via the Docker awslogs driver if you want it on AWS)."""
from __future__ import annotations

import json
import logging
import sys
import time

from prometheus_client import Counter, Gauge, Histogram

STAGE_BUCKETS = (50, 100, 200, 300, 500, 750, 1000, 1500, 2000, 2500, 3000, 4000, 6000, 10000)

STAGE_LATENCY = Histogram("mitra_stage_latency_ms", "Per-stage latency (ms)", ["stage"], buckets=STAGE_BUCKETS)
TURNS = Counter("mitra_turns_total", "Conversation turns by outcome", ["outcome"])
PROVIDER_ERRORS = Counter("mitra_provider_errors_total", "Provider failures", ["kind", "provider"])
WAKE_EVENTS = Counter("mitra_wake_events_total", "Wake activations", ["engine"])
CONNECTED = Gauge("mitra_devices_connected", "Devices with an open audio WebSocket")
AUDIO_IN_SECONDS = Counter("mitra_audio_in_seconds_total", "Uplink audio received (s)", ["phase"])
TTS_CHARS = Counter("mitra_tts_characters_total", "Characters sent to TTS", ["provider"])
STT_SECONDS = Counter("mitra_stt_seconds_total", "Audio seconds sent to STT", ["provider"])


class JsonFormatter(logging.Formatter):
    _skip = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in self._skip and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable lines that still show the `extra={...}` fields (device, err, ...)."""
    _skip = JsonFormatter._skip

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = " ".join(f"{k}={v}" for k, v in record.__dict__.items()
                          if k not in self._skip and not k.startswith("_"))
        return f"{line}  [{extras}]" if extras else line


def setup_logging(level: str = "INFO", as_json: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if as_json else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel("WARNING")
