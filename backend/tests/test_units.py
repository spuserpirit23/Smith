import asyncio
import time

import pytest

from mitra import auth
from mitra.audio.pcm import tone
from mitra.audio.vad import EndpointDetector
from mitra.providers.base import ProviderError
from mitra.providers.tts import FallbackTTS, MockTTS
from mitra.providers.vision import parse_vision_json
from mitra.repo import summarize_latency
from mitra.session.text import SentenceChunker, clean_for_speech
from mitra.sim import silence, speech_like


def test_sentence_chunker_releases_first_sentence_early():
    c = SentenceChunker()
    out = []
    for tok in ["Hi", " there", "! I am", " Mitra, a", " robot.", " How are", " you?"]:
        out += c.feed(tok)
    out += c.flush()
    assert out[0] == "Hi there!"
    assert " ".join(out) == "Hi there! I am Mitra, a robot. How are you?"


def test_clean_for_speech_strips_markdown():
    assert clean_for_speech("**Hello** _world_ [link](http://x)") == "Hello world link"


def test_endpoint_detector_finds_start_and_end():
    vad = EndpointDetector(end_silence_ms=600)
    events = vad.feed(silence(500) + speech_like(1200) + silence(800))
    kinds = [e.kind for e in events]
    assert kinds == ["speech_start", "speech_end"]
    assert 1500 < events[1].at_ms < 2600


def test_endpoint_detector_ignores_silence():
    vad = EndpointDetector()
    assert vad.feed(silence(3000)) == []


def test_mosquitto_hash_roundtrip():
    h = auth.mosquitto_hash("s3cret")
    assert h.startswith("$7$101$")
    assert auth.verify_mosquitto_hash("s3cret", h)
    assert not auth.verify_mosquitto_hash("nope", h)


def test_signed_tokens_expire_and_bind_subject():
    t = auth.sign("k", "upload", "cam/1", 5)
    assert auth.verify("k", "upload", "cam/1", t)
    assert not auth.verify("k", "upload", "cam/2", t)
    assert not auth.verify("other", "upload", "cam/1", t)
    expired = f"{int(time.time()) - 1}.{t.split('.')[1]}"
    assert not auth.verify("k", "upload", "cam/1", expired)


def test_device_id_validation():
    assert auth.valid_device_id("mitra-001")
    for bad in ["a", "Mitra", "a/b", "x+y", "#all", "-abc"]:
        assert not auth.valid_device_id(bad)


def test_latency_summary():
    rows = [{"outcome": "ok", "end_to_first_audio_ms": v} for v in range(100, 2100, 100)]
    s = summarize_latency(rows)
    assert s["turns"] == 20 and s["outcomes"] == {"ok": 20}
    assert s["end_to_first_audio_ms"]["median"] == 1050
    assert s["end_to_first_audio_ms"]["p95"] == 1900


async def test_tts_fallback_on_error():
    tts = FallbackTTS([MockTTS(fail=True), MockTTS()])
    chunks = [c async for c in tts.synthesize("hello there", "en-IN")]
    assert chunks and tts.last_used == "mock"


async def test_tts_fallback_on_slow_first_chunk():
    slow = MockTTS(delay_s=1.0)
    slow.name = "slow"
    tts = FallbackTTS([slow, MockTTS()], first_chunk_timeout_s=0.2)
    chunks = [c async for c in tts.synthesize("hello", "en-IN")]
    assert chunks and tts.last_used == "mock"


async def test_tts_all_fail():
    tts = FallbackTTS([MockTTS(fail=True)])
    with pytest.raises(ProviderError):
        _ = [c async for c in tts.synthesize("x", "en")]


def test_vision_json_parsing():
    r = parse_vision_json('Sure: {"description": "A cup.", "confidence": 0.8, "person_present": false}')
    assert r.description == "A cup." and r.confidence == 0.8 and r.person_present is False
    assert parse_vision_json("no json here").confidence == 0.5


def test_tone_length():
    assert len(tone(440, 100)) == 3200
    asyncio.get_event_loop_policy()  # keep import used


def _run_think_filter(deltas: list[str]) -> str:
    from mitra.providers.llm import ThinkFilter

    f = ThinkFilter()
    return "".join(f.feed(d) for d in deltas) + f.flush()


def test_think_filter_drops_reasoning_split_across_deltas():
    deltas = ["<thi", "nk>The user is", " lonely.</th", "ink>\n\n", "I'm here", " for you."]
    assert _run_think_filter(deltas) == "I'm here for you."


def test_think_filter_passes_plain_text_and_lone_angle_brackets():
    assert _run_think_filter(["2 < 3", " and ", "5 > 4."]) == "2 < 3 and 5 > 4."
    assert _run_think_filter(["Hello", " <"]) == "Hello <"


def test_think_filter_unclosed_think_yields_nothing():
    assert _run_think_filter(["<think>still reasoning when max_tokens hit"]) == ""


def test_piper_voice_routing_by_script_and_language():
    from mitra.providers.tts import pick_voice_key

    both = ["default", "hi"]
    assert pick_voice_key("नमस्ते, आप कैसे हैं?", "en-IN", both) == "hi"
    assert pick_voice_key("Hello there!", "en-IN", both) == "default"
    assert pick_voice_key("Aap kaise ho?", "hi-IN", both) == "default"  # Latin script: English voice
    assert pick_voice_key("नमस्ते", "en-IN", ["default"]) == "default"   # no Hindi voice installed


def test_clean_for_speech_strips_emoji_keeps_devanagari():
    assert clean_for_speech("Did you wear a hat? 🍹🌞") == "Did you wear a hat?"
    assert clean_for_speech("Great 👍🏽 job ❤️!") == "Great job !"
    assert clean_for_speech("नमस्ते! आप कैसे हैं?") == "नमस्ते! आप कैसे हैं?"


def test_think_filter_flags_prefilled_reasoning_and_drops_stray_close_tag():
    from mitra.providers.llm import ThinkFilter

    f = ThinkFilter()
    out = "".join(f.feed(d) for d in ["Okay, the user", " said hi.</th", "ink>\n\nHello!"]) + f.flush()
    assert f.leaked and "</think>" not in out and out.endswith("Hello!")
