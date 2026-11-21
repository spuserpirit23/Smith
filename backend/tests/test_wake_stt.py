"""STT keyword wake ("Smith"): segmentation of idle audio and keyword matching."""
import asyncio

import numpy as np

from mitra.providers.wake import SttKeywordDetector, SttKeywordFactory


def tone(db: float, ms: int) -> bytes:
    n = 16 * ms
    amp = 32768 * 10 ** (db / 20) * np.sqrt(2)
    return (amp * np.sin(np.arange(n) * 2 * np.pi * 300 / 16000)).astype("<i2").tobytes()


def test_segments_speech_in_a_noisy_room():
    d = SttKeywordFactory(["smith"]).create()
    room = tone(-30, 1500)                    # noisy room, like the real board (-27..-37 dBFS)
    segs = d.feed(room)
    assert segs == []                         # background alone is never a segment
    segs = d.feed(tone(-12, 700) + room)      # "Smith" then quiet
    assert len(segs) == 1
    seconds = len(segs[0]) / 32000
    assert 0.7 <= seconds <= 1.6              # word + preroll + end silence, not the whole room


def test_long_speech_is_cut_into_bounded_segments():
    d = SttKeywordDetector(["smith"], max_segment_s=2.0)
    d.feed(tone(-30, 1000))
    segs = d.feed(tone(-12, 5000))
    assert segs and all(len(s) <= 2.0 * 32000 + 640 for s in segs)


def test_keyword_matching_tolerates_whisper_spellings():
    d = SttKeywordDetector(["smith"])
    for text in ["Smith.", "Hey Smith, what's up?", "smyth", "Smit!", "OK SMITH"]:
        assert d.matches(text), text
    for text in ["", "blacksmith tools", "Smithsonian", "with", "what is two plus two"]:
        assert not d.matches(text), text


def test_multiple_words():
    d = SttKeywordFactory(["smith", "mitra"]).create()
    assert d.matches("hey mitra") and d.matches("smith")


async def test_saying_smith_wakes_and_other_speech_does_not(stack):
    """End to end over the real WebSocket: idle speech is transcribed; only "Smith" wakes."""
    from mitra.providers.stt import MockSTT
    from mitra.sim import SimDevice, silence, speech_like

    stack.rt.p.wake = SttKeywordFactory(["smith"])
    secret = await stack.provision("mitra-001")
    dev = SimDevice(stack.ws_url, "mitra-001", secret, wake="cloud", speed=4)
    await dev.connect()
    assert dev.hello["wake"] == "cloud"

    # 1. Someone talking nearby: transcribed, no wake.
    MockSTT.script[:] = ["the weather is nice today"]
    await dev.stream(silence(600) + speech_like(800) + silence(700))
    for _ in range(50):
        if not MockSTT.script:
            break
        await asyncio.sleep(0.05)
    assert MockSTT.script == []
    await asyncio.sleep(0.2)
    assert not [m for m in dev.log if m["type"] == "wake"]

    # 2. "Smith": wakes and opens a listening turn; the question is then transcribed normally.
    MockSTT.script[:] = ["Smith.", "what is two plus two"]
    await dev.stream(speech_like(600) + silence(700))
    wake = await dev.wait_for("wake")
    assert wake["engine"] == "stt"
    await dev.wait_for("state", lambda m: m["state"] == "listening")
    await dev.stream(silence(200) + speech_like(1000) + silence(900))
    stt = await dev.wait_for("stt")
    assert stt["text"] == "what is two plus two"
    await dev.close()
