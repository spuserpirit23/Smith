"""Speech-to-text providers."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import AsyncIterator

import httpx

from ...audio.pcm import rms_dbfs, to_wav
from ..base import ProviderError, Transcript

log = logging.getLogger(__name__)


async def collect(audio: AsyncIterator[bytes]) -> bytes:
    parts = [chunk async for chunk in audio]
    return b"".join(parts)


def iso639_1(language: str) -> str:
    return language.split("-")[0].lower()


class AwsTranscribeSTT:
    """Amazon Transcribe streaming. Credentials from the default AWS chain (EC2 instance role)."""

    name = "aws_transcribe"

    def __init__(self, region: str):
        try:
            from amazon_transcribe.client import TranscribeStreamingClient  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise ProviderError("amazon-transcribe not installed (pip install '.[aws]')") from e
        self.region = region

    async def transcribe(self, audio: AsyncIterator[bytes], language: str) -> Transcript:
        from amazon_transcribe.client import TranscribeStreamingClient
        from amazon_transcribe.handlers import TranscriptResultStreamHandler

        finals: list[str] = []
        confs: list[float] = []

        class Handler(TranscriptResultStreamHandler):
            async def handle_transcript_event(self, event):  # type: ignore[override]
                for result in event.transcript.results:
                    if result.is_partial or not result.alternatives:
                        continue
                    alt = result.alternatives[0]
                    finals.append(alt.transcript)
                    for item in alt.items or []:
                        if getattr(item, "confidence", None) is not None:
                            confs.append(float(item.confidence))

        client = TranscribeStreamingClient(region=self.region)
        try:
            stream = await client.start_stream_transcription(
                language_code=language, media_sample_rate_hz=16000, media_encoding="pcm"
            )

            async def writer() -> None:
                async for chunk in audio:
                    # Transcribe prefers chunks of ~50-200 ms; device frames are 20-60 ms, fine.
                    await stream.input_stream.send_audio_event(audio_chunk=chunk)
                await stream.input_stream.end_stream()

            await asyncio.gather(writer(), Handler(stream.output_stream).handle_events())
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"transcribe failed: {e}") from e
        text = " ".join(t.strip() for t in finals if t.strip())
        conf = sum(confs) / len(confs) if confs else (1.0 if text else 0.0)
        return Transcript(text=text, confidence=conf, language=language)


class OpenAICompatSTT:
    """POST /audio/transcriptions — OpenAI, Groq, a self-hosted faster-whisper-server, etc."""

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=30)

    async def transcribe(self, audio: AsyncIterator[bytes], language: str) -> Transcript:
        pcm = await collect(audio)
        if not pcm:
            return Transcript("", 0.0, language)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = await self._client.post(
                f"{self.base_url}/audio/transcriptions",
                headers=headers,
                files={"file": ("audio.wav", to_wav(pcm), "audio/wav")},
                data={
                    "model": self.model,
                    "language": iso639_1(language),
                    "response_format": "verbose_json",
                    "temperature": "0",
                },
            )
            r.raise_for_status()
            body = r.json()
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"stt http failed: {e}") from e
        text = (body.get("text") or "").strip()
        return Transcript(text, _whisper_confidence(body.get("segments"), text), language)


def _whisper_confidence(segments: list[dict] | None, text: str) -> float:
    if not text:
        return 0.0
    if not segments:
        return 1.0
    logprobs = [s.get("avg_logprob") for s in segments if s.get("avg_logprob") is not None]
    nospeech = [s.get("no_speech_prob", 0.0) or 0.0 for s in segments]
    conf = math.exp(sum(logprobs) / len(logprobs)) if logprobs else 1.0
    if nospeech and min(nospeech) > 0.6:
        conf *= 0.3
    return max(0.0, min(1.0, conf))


class GoogleSTT:
    """Google Web Speech via the ``SpeechRecognition`` library: free, no API key, no local model.

    Uses the same endpoint Chrome's speech input uses. Fine for testing and light use; for
    production volume prefer a keyed provider (aws_transcribe / openai_compat).
    """

    name = "google"

    def __init__(self, api_key: str | None = None, sample_rate: int = 16000,
                 request_timeout_s: float = 3.5, attempts: int = 2):
        try:
            import speech_recognition as sr
        except ImportError as e:  # pragma: no cover
            raise ProviderError("SpeechRecognition not installed (pip install '.[speech]')") from e
        self._sr = sr
        self._recognizer = sr.Recognizer()
        # The library's default is no timeout at all: one stalled request hangs the whole turn.
        # Short per-request timeout + one retry fits inside the turn's stt budget (8 s).
        self._recognizer.operation_timeout = request_timeout_s
        self.attempts = max(1, attempts)
        self.api_key = api_key or None
        self.sample_rate = sample_rate
        # Each request first encodes FLAC by running the bundled flac binary. The first run after
        # startup can be slow on Windows (antivirus scan / cold disk), so pay it now, offline.
        try:
            sr.AudioData(b"\x00\x00" * 1600, sample_rate, 2).get_flac_data()
        except Exception as e:  # noqa: BLE001
            log.warning("google stt: flac warm-up failed", extra={"err": repr(e)})

    async def transcribe(self, audio: AsyncIterator[bytes], language: str) -> Transcript:
        sr = self._sr
        pcm = await collect(audio)
        if not pcm:
            return Transcript("", 0.0, language)
        data = sr.AudioData(pcm, self.sample_rate, 2)  # 16-bit mono PCM

        def once() -> Transcript:
            try:
                result = self._recognizer.recognize_google(
                    data, key=self.api_key, language=language, show_all=True
                )
            except sr.UnknownValueError:  # nothing intelligible -> empty transcript (repeat prompt)
                return Transcript("", 0.0, language)
            # show_all=True returns [] (nothing recognised) or {"alternative": [...], ...}
            if not result or not isinstance(result, dict) or not result.get("alternative"):
                return Transcript("", 0.0, language)
            alts = result["alternative"]
            best = next((a for a in alts if "confidence" in a), alts[0])
            text = (best.get("transcript") or "").strip()
            conf = float(best.get("confidence", 0.9 if text else 0.0))
            return Transcript(text, conf if text else 0.0, language)

        def run() -> Transcript:
            last: Exception | None = None
            for attempt in range(1, self.attempts + 1):
                t0 = time.monotonic()
                try:
                    return once()
                except (sr.RequestError, TimeoutError, OSError) as e:  # network error / stall
                    last = e
                    log.warning("google stt request failed", extra={
                        "attempt": attempt, "secs": round(time.monotonic() - t0, 2), "err": repr(e)})
            raise ProviderError(f"google stt failed: {last}") from last

        return await asyncio.to_thread(run)


class MockSTT:
    """Deterministic STT for tests and the device simulator.

    Returns queued transcripts in order (``MockSTT.script``), or ``default_text``. Silent audio
    yields an empty transcript so the repeat-prompt path can be exercised.
    """

    name = "mock"
    script: list[str] = []

    def __init__(self, default_text: str = "hello mitra, how are you today?", delay_s: float = 0.05):
        self.default_text = default_text
        self.delay_s = delay_s

    async def transcribe(self, audio: AsyncIterator[bytes], language: str) -> Transcript:
        pcm = await collect(audio)
        await asyncio.sleep(self.delay_s)
        if not pcm or rms_dbfs(pcm) < -55:
            return Transcript("", 0.0, language)
        text = MockSTT.script.pop(0) if MockSTT.script else self.default_text
        if text == "__slow__":
            await asyncio.sleep(3600)
        return Transcript(text, 0.95 if text else 0.0, language)
