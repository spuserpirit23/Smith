"""Build provider instances from config. The only place that knows concrete classes."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig
from .base import LLMProvider, ObjectStore, STTProvider, TTSProvider, VisionProvider, WakeWordFactory
from .llm import AnthropicLLM, BedrockLLM, MockLLM, OpenAICompatLLM
from .storage import LocalStore, S3Store
from .stt import AwsTranscribeSTT, GoogleSTT, MockSTT, OpenAICompatSTT
from .tts import FallbackTTS, MockTTS, OpenAICompatTTS, PiperLocalTTS, PollyTTS
from .vision import AnthropicVision, BedrockVision, MockVision, OpenAICompatVision, RekognitionVision
from .wake import MockWakeFactory, OpenWakeWordFactory, SttKeywordFactory


@dataclass
class Providers:
    stt: STTProvider
    llm: LLMProvider
    tts: FallbackTTS
    wake: WakeWordFactory | None
    vision: VisionProvider | None
    storage: ObjectStore


def build_stt(cfg: AppConfig) -> STTProvider:
    kind = cfg.get("stt.provider")
    c = cfg.section(f"stt.{kind}")
    if kind == "aws_transcribe":
        return AwsTranscribeSTT(region=c["region"])
    if kind == "openai_compat":
        return OpenAICompatSTT(c["base_url"], c.get("api_key", ""), c["model"])
    if kind == "google":
        return GoogleSTT(c.get("api_key") or None)
    if kind == "mock":
        return MockSTT()
    raise ValueError(f"unknown stt provider {kind!r}")


def build_llm(cfg: AppConfig) -> LLMProvider:
    kind = cfg.get("llm.provider")
    c = cfg.section(f"llm.{kind}")
    mt, temp = int(cfg.get("llm.max_tokens", 300)), float(cfg.get("llm.temperature", 0.6))
    if kind == "bedrock":
        return BedrockLLM(c["region"], c["model_id"], mt, temp)
    if kind == "anthropic":
        return AnthropicLLM(c.get("api_key", ""), c["model"], mt, temp)
    if kind == "openai_compat":
        return OpenAICompatLLM(c["base_url"], c.get("api_key", ""), c["model"], mt, temp,
                               c.get("reasoning_effort", "") or "")
    if kind == "mock":
        return MockLLM()
    raise ValueError(f"unknown llm provider {kind!r}")


def _one_tts(cfg: AppConfig, kind: str) -> TTSProvider:
    c = cfg.section(f"tts.{kind}")
    if kind == "polly":
        return PollyTTS(c["region"], c["voice_id"], c.get("engine", "neural"))
    if kind == "openai_compat":
        return OpenAICompatTTS(c["base_url"], c.get("api_key", ""), c["model"], c["voice"])
    if kind == "piper_local":
        voices = {k: v for k, v in (c.get("voices") or {}).items() if v}
        return PiperLocalTTS(c["model_path"], voices)
    if kind == "mock":
        return MockTTS()
    raise ValueError(f"unknown tts provider {kind!r}")


def build_tts(cfg: AppConfig) -> FallbackTTS:
    kinds = cfg.get("tts.providers", "polly")
    if isinstance(kinds, str):
        kinds = [k.strip() for k in kinds.split(",") if k.strip()]
    budget = float(cfg.get("conversation.timeouts.tts_s", 8)) / 2
    return FallbackTTS([_one_tts(cfg, k) for k in kinds], first_chunk_timeout_s=budget)


def build_wake(cfg: AppConfig) -> WakeWordFactory | None:
    kind = cfg.get("wake.provider", "none")
    if kind in (None, "", "none"):
        return None
    if kind == "openwakeword":
        return OpenWakeWordFactory(cfg.get("wake.model"), float(cfg.get("wake.threshold", 0.5)),
                                   float(cfg.get("wake.refractory_s", 2.0)))
    if kind == "mock":
        return MockWakeFactory()
    if kind == "stt":
        words = cfg.get("wake.words", "smith")
        if isinstance(words, str):
            words = [w.strip() for w in words.split(",") if w.strip()]
        return SttKeywordFactory(words)
    raise ValueError(f"unknown wake provider {kind!r}")


def build_vision(cfg: AppConfig) -> VisionProvider | None:
    kind = cfg.get("vision.provider", "none")
    c = cfg.section(f"vision.{kind}")
    if kind in (None, "", "none"):
        return None
    if kind == "bedrock":
        return BedrockVision(c["region"], c["model_id"])
    if kind == "anthropic":
        return AnthropicVision(c.get("api_key", ""), c["model"])
    if kind == "openai_compat":
        return OpenAICompatVision(c["base_url"], c.get("api_key", ""), c["model"], c.get("reasoning_effort", "") or "",
                                  prompt_style=c.get("prompt_style", "json") or "json",
                                  timeout_s=float(c.get("timeout_s", 20) or 20))
    if kind == "rekognition":
        return RekognitionVision(c["region"])
    if kind == "mock":
        return MockVision()
    raise ValueError(f"unknown vision provider {kind!r}")


def build_storage(cfg: AppConfig) -> ObjectStore:
    kind = cfg.get("storage.provider", "local")
    c = cfg.section(f"storage.{kind}")
    if kind == "local":
        return LocalStore(c.get("root", "/data"))
    if kind == "s3":
        return S3Store(c.get("bucket", ""), c.get("region", "ap-south-1"), c.get("endpoint_url"))
    raise ValueError(f"unknown storage provider {kind!r}")


def build_providers(cfg: AppConfig) -> Providers:
    return Providers(
        stt=build_stt(cfg),
        llm=build_llm(cfg),
        tts=build_tts(cfg),
        wake=build_wake(cfg),
        vision=build_vision(cfg),
        storage=build_storage(cfg),
    )
