"""Text-to-speech providers. Output is always pcm16 @ 16 kHz mono.

``FallbackTTS`` wraps an ordered list (FR-6.1/6.5): the primary voice goes first (Piper on the
box by default), any other voice after it, and the chain falls through on error or when the
first audio chunk misses its latency budget.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import AsyncIterator, Iterable

import httpx

from ...audio.pcm import resample, tone
from ..base import ProviderError, TTSProvider
from ..llm import iterate_in_thread

log = logging.getLogger(__name__)
CHUNK = 3200  # 100 ms


class PollyTTS:
    name = "polly"

    def __init__(self, region: str, voice_id: str, engine: str = "neural"):
        try:
            import boto3
        except ImportError as e:  # pragma: no cover
            raise ProviderError("boto3 not installed (pip install '.[aws]')") from e
        self._client = boto3.client("polly", region_name=region)
        self.voice_id = voice_id
        self.engine = engine

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        def make_iter():
            resp = self._client.synthesize_speech(
                Text=text,
                OutputFormat="pcm",
                SampleRate="16000",
                VoiceId=self.voice_id,
                Engine=self.engine,
                LanguageCode=language,
            )
            stream = resp["AudioStream"]
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                yield chunk

        async for chunk in iterate_in_thread(make_iter):
            yield chunk


class OpenAICompatTTS:
    """POST /audio/speech with response_format=pcm (OpenAI returns 24 kHz pcm16)."""

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, voice: str, source_rate: int = 24000):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.source_rate = source_rate
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5))

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "voice": self.voice, "input": text, "response_format": "pcm"}
        carry = b""
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/audio/speech", json=body, headers=headers
            ) as r:
                if r.status_code >= 400:
                    raise ProviderError(f"tts http {r.status_code}")
                async for raw in r.aiter_bytes(4800):
                    raw = carry + raw
                    usable = len(raw) // 2 * 2
                    carry = raw[usable:]
                    if usable:
                        yield resample(raw[:usable], self.source_rate)
        except httpx.HTTPError as e:
            raise ProviderError(f"tts stream failed: {e}") from e


_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def pick_voice_key(text: str, language: str, available: Iterable[str]) -> str:
    """Choose a voice key ("default" or an ISO-639-1 code) for one sentence.

    The LLM replies in whatever language the user spoke, so the session language alone is not
    enough: a Devanagari sentence goes to the Hindi voice even in an en-IN session.
    """
    keys = set(available)
    if "hi" in keys and _DEVANAGARI.search(text):
        return "hi"
    lang = (language or "").split("-")[0].lower()
    if lang in keys and lang != "hi":  # never send Latin-script text to the Hindi voice
        return lang
    return "default"


class PiperLocalTTS:
    """Piper neural TTS on the host CPU: no API, no per-character cost.

    ~30x faster than real time on a laptop CPU, well under real time on 2 vCPU.
    ``voices`` maps extra ISO-639-1 codes to models (e.g. {"hi": ".../hi_IN-pratham-medium.onnx"});
    ``model_path`` is the default voice.
    """

    name = "piper_local"

    def __init__(self, model_path: str, voices: dict[str, str] | None = None):
        try:
            from piper import PiperVoice
        except ImportError as e:  # pragma: no cover
            raise ProviderError("piper-tts not installed (pip install '.[local]')") from e
        self._voices = {"default": PiperVoice.load(model_path)}
        for lang, path in (voices or {}).items():
            if not path:
                continue
            try:
                self._voices[lang.lower()] = PiperVoice.load(path)
            except Exception as e:  # noqa: BLE001  (a missing extra voice must not kill startup)
                log.warning("piper: extra voice not loaded", extra={"lang": lang, "path": path, "err": str(e)})
        # The first inference is slow (ONNX graph warm-up) and would blow the first-chunk budget,
        # dropping the first reply (e.g. the greeting) to the fallback voice. Pay it at startup.
        for voice in self._voices.values():
            try:
                if hasattr(voice, "synthesize_stream_raw"):  # piper-tts <= 1.2
                    for _ in voice.synthesize_stream_raw("Hello."):
                        pass
                else:
                    for _ in voice.synthesize("Hello."):
                        pass
            except Exception:  # noqa: BLE001  (warm-up is best effort)
                pass

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        voice = self._voices[pick_voice_key(text, language, self._voices)]

        def make_iter():
            if hasattr(voice, "synthesize_stream_raw"):  # piper-tts <= 1.2
                rate = voice.config.sample_rate
                for raw in voice.synthesize_stream_raw(text):
                    yield resample(raw, rate)
            else:  # piper-tts >= 1.3
                for chunk in voice.synthesize(text):
                    yield resample(chunk.audio_int16_bytes, chunk.sample_rate)

        async for chunk in iterate_in_thread(make_iter):
            yield chunk


class MockTTS:
    """A short tone per word. 'tts fail' in the text raises, for fallback tests."""

    name = "mock"

    def __init__(self, fail: bool = False, delay_s: float = 0.0):
        self.fail = fail
        self.delay_s = delay_s

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        if self.fail or "tts fail" in text.lower():
            raise ProviderError("injected tts failure")
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        for i, _word in enumerate(text.split()):
            yield tone(440 + 40 * (i % 5), 120, amplitude=0.2)


class FallbackTTS:
    name = "fallback"

    def __init__(self, providers: list[TTSProvider], first_chunk_timeout_s: float = 3.0):
        if not providers:
            raise ValueError("at least one TTS provider required")
        self.providers = providers
        self.first_chunk_timeout_s = first_chunk_timeout_s
        self.last_used: str | None = None

    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        last_err: Exception | None = None
        for provider in self.providers:
            gen = provider.synthesize(text, language).__aiter__()
            try:
                first = await asyncio.wait_for(gen.__anext__(), self.first_chunk_timeout_s)
            except StopAsyncIteration:
                self.last_used = provider.name
                return
            except (ProviderError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
                last_err = e
                log.warning("tts provider failed, falling back", extra={"provider": provider.name, "err": str(e)})
                await _aclose(gen)
                continue
            self.last_used = provider.name
            yield first
            async for chunk in gen:
                yield chunk
            return
        raise ProviderError(f"all TTS providers failed: {last_err}")


async def _aclose(gen) -> None:
    close = getattr(gen, "aclose", None)
    if close:
        try:
            await close()
        except Exception:  # noqa: BLE001
            pass
