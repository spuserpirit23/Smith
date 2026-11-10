"""Provider interfaces (PRD NFR "Portability").

Every external capability sits behind one of these protocols, so swapping AWS for another
vendor or a self-hosted model is a config change. Implementations live in sibling packages
and are registered in ``registry.py``.

Audio convention everywhere: 16 kHz, 16-bit signed little-endian, mono PCM ("pcm16").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, runtime_checkable

SAMPLE_RATE = 16000


class ProviderError(RuntimeError):
    """Raised by a provider for any failure the pipeline should treat as recoverable."""


@dataclass
class Transcript:
    text: str
    confidence: float  # 0..1; providers that don't report one return 1.0 for non-empty text
    language: str | None = None


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class VisionResult:
    description: str          # short natural-language summary for the LLM
    confidence: float         # 0..1 overall
    labels: list[tuple[str, float]] = field(default_factory=list)
    person_present: bool | None = None


@runtime_checkable
class STTProvider(Protocol):
    name: str

    async def transcribe(self, audio: AsyncIterator[bytes], language: str) -> Transcript:
        """Consume pcm16 chunks until the iterator ends, return the final transcript.

        Streaming providers should start sending as chunks arrive so the result is ready
        shortly after the iterator closes; batch providers may buffer.
        """
        ...


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def stream(self, system: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        """Yield text deltas of the assistant reply."""
        ...


@runtime_checkable
class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        """Yield pcm16 @ 16 kHz chunks for ``text``."""
        ...


@runtime_checkable
class WakeWordDetector(Protocol):
    """One instance per audio stream (stateful)."""

    name: str

    def process(self, pcm16: bytes) -> float | None:
        """Feed audio; return a confidence score when the wake phrase fires, else None."""
        ...

    def reset(self) -> None: ...


class WakeWordFactory(Protocol):
    name: str

    def create(self) -> WakeWordDetector: ...


@runtime_checkable
class VisionProvider(Protocol):
    name: str

    async def describe(self, jpeg: bytes, question: str) -> VisionResult: ...


@runtime_checkable
class ObjectStore(Protocol):
    name: str

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete_prefix(self, prefix: str) -> int: ...
