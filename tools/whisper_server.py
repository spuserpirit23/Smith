"""Local speech-to-text server for Mitra: faster-whisper behind an OpenAI-compatible
``POST /v1/audio/transcriptions`` endpoint, so the backend's ``openai_compat`` STT provider can use
it with no code change (see docs/LOCAL_MODELS.md, "STT").

Run it in its own venv (faster-whisper / CTranslate2 wheels are most reliable on Python 3.11):

    uv venv --python 3.11 %LOCALAPPDATA%/mitra/whisper-venv
    uv pip install --python %LOCALAPPDATA%/mitra/whisper-venv/Scripts/python.exe \
        faster-whisper fastapi "uvicorn[standard]" python-multipart
    %LOCALAPPDATA%/mitra/whisper-venv/Scripts/python.exe tools/whisper_server.py

backend/.env:
    MITRA_STT_PROVIDER=openai_compat
    STT_BASE_URL=http://localhost:8000/v1
    STT_MODEL=small

Env vars: WHISPER_MODEL (default "small"; tiny|base|small|medium|large-v3|distil-large-v3 or a
local CTranslate2 dir), WHISPER_DEVICE (cpu|cuda, default cpu), WHISPER_COMPUTE (default int8),
WHISPER_THREADS (default: all cores), WHISPER_HOST / WHISPER_PORT (default 127.0.0.1:8000).
The model is downloaded from Hugging Face on first start and cached after that.
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("whisper_server")

MODEL = os.environ.get("WHISPER_MODEL", "small")
DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8")
THREADS = int(os.environ.get("WHISPER_THREADS", "0"))

log.info("loading whisper model %s (%s, %s)...", MODEL, DEVICE, COMPUTE)
_t0 = time.monotonic()
model = WhisperModel(MODEL, device=DEVICE, compute_type=COMPUTE, cpu_threads=THREADS)
log.info("model loaded in %.1f s", time.monotonic() - _t0)
_lock = threading.Lock()  # one transcription at a time; CTranslate2 already uses every core

app = FastAPI(title="mitra-whisper")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "model": MODEL}


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


@app.post("/v1/audio/transcriptions")
def transcriptions(
    file: UploadFile = File(...),
    model_name: str = Form("", alias="model"),  # accepted for API compatibility; the loaded model is used
    language: str = Form(""),
    prompt: str = Form(""),
    response_format: str = Form("json"),
    temperature: float = Form(0.0),
) -> dict:
    audio = io.BytesIO(file.file.read())
    t0 = time.monotonic()
    with _lock:
        segments, info = model.transcribe(
            audio,
            language=language or None,
            initial_prompt=prompt or None,
            temperature=temperature,
            beam_size=1,  # greedy: much faster on CPU, near-identical accuracy on short utterances
            vad_filter=True,
            condition_on_previous_text=False,
        )
        segs = list(segments)
    text = " ".join(s.text.strip() for s in segs).strip()
    log.info("transcribed %.1f s audio in %.2f s: %r", info.duration, time.monotonic() - t0, text)
    if response_format != "verbose_json":
        return {"text": text}
    return {
        "task": "transcribe",
        "language": info.language,
        "duration": info.duration,
        "text": text,
        "segments": [
            {"id": i, "start": s.start, "end": s.end, "text": s.text,
             "avg_logprob": s.avg_logprob, "no_speech_prob": s.no_speech_prob}
            for i, s in enumerate(segs)
        ],
    }


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("WHISPER_HOST", "127.0.0.1"),
                port=int(os.environ.get("WHISPER_PORT", "8000")), log_level="warning")
