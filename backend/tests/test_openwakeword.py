"""Cloud wake with the real openWakeWord engine (stock "hey_jarvis" model standing in for the
not-yet-trained "Hey Smith"), driven by espeak-ng speech. Opt-in: MITRA_TEST_OWW=1."""
import os
import shutil
import subprocess

import pytest

from mitra.audio.pcm import resample
from mitra.sim import SimDevice, silence

pytestmark = pytest.mark.skipif(not os.environ.get("MITRA_TEST_OWW") or not shutil.which("espeak-ng"),
                                reason="set MITRA_TEST_OWW=1 (needs openwakeword + espeak-ng)")


def say(text: str) -> bytes:
    wav = subprocess.run(["espeak-ng", "-v", "en-gb", "-s", "150", "--stdout", text],
                         capture_output=True, check=True).stdout
    pcm = wav[44:][: (len(wav) - 44) // 2 * 2]
    return resample(pcm, int.from_bytes(wav[24:28], "little"))


@pytest.mark.parametrize("stack", ["wake: {provider: openwakeword, model: hey_jarvis, threshold: 0.5}\n"],
                         indirect=True)
async def test_real_wake_engine_end_to_end(stack):
    secret = await stack.provision("mitra-001")
    dev = SimDevice(stack.ws_url, "mitra-001", secret, wake="cloud", speed=2)
    await dev.connect()
    assert dev.hello["wake"] == "cloud"
    # Ordinary talk (including the company name) must not wake it.
    await dev.stream(silence(300) + say("Smith and Sky make lovely clothes") + silence(500))
    assert not [m for m in dev.log if m["type"] == "wake"]
    await dev.stream(say("hey jarvis") + silence(400))
    wake = await dev.wait_for("wake", timeout=5)
    assert wake["engine"] == "cloud" and wake["confidence"] >= 0.5
    await dev.stream(say("what is the weather like") + silence(1200))
    await dev.wait_for("stt", timeout=8)
    await dev.wait_for("tts", lambda m: m.get("state") == "stop", timeout=10)
    await dev.close()
