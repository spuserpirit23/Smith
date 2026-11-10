"""Software stand-in for the ESP32 main board. Speaks protocol v1 exactly like the firmware,
so the backend can be developed, load-tested and demoed without hardware.

  mitra-sim --url ws://localhost:8080/v1/device/ws --device mitra-001 --secret ... \
            --mode ptt --cycles 20 --wav question.wav --save-replies out/

Modes: ptt (button), cloud (sends a 1 kHz "wake tone" that the mock wake engine accepts, or a
real "Hey Smith" recording with --wake-wav when openWakeWord is configured).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from .audio.pcm import from_wav, resample, to_wav, tone

FRAME = 640  # 20 ms


def speech_like(ms: int = 1200) -> bytes:
    """Voiced-ish test signal loud enough for VAD; not intelligible speech."""
    parts = [tone(f, ms // 6, amplitude=0.35) for f in (220, 260, 300, 240, 280, 230)]
    return b"".join(parts)


def silence(ms: int) -> bytes:
    return b"\x00\x00" * (16 * ms)


def wake_tone(ms: int = 500) -> bytes:
    return tone(1000, ms, amplitude=0.6)


class SimDevice:
    def __init__(self, url: str, device_id: str, secret: str, wake: str = "ptt", speed: float = 1.0,
                 auto_playback_done: bool = True, camera: bool = False,
                 extra_hello: dict[str, Any] | None = None):
        self.extra_hello = extra_hello or {}
        self.camera = camera
        self.url, self.device_id, self.secret = url, device_id, secret
        self.wake = wake
        self.speed = speed
        self.auto_playback_done = auto_playback_done
        self.ws = None
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.log: list[dict[str, Any]] = []
        self.reply_audio = bytearray()
        self.replies: list[bytes] = []
        self._t_first_audio: float | None = None
        self._rx: asyncio.Task | None = None
        self.hello: dict[str, Any] = {}

    async def connect(self) -> None:
        import websockets

        self.ws = await websockets.connect(
            self.url, additional_headers={"X-Device-Id": self.device_id,
                                          "Authorization": f"Bearer {self.secret}"},
            max_size=2**20)
        await self.ws.send(json.dumps({
            "type": "hello", "version": 1, "fw": "sim-0.1.0", "wake": self.wake,
            **({"camera": True} if self.camera else {}),
            **self.extra_hello,
            "audio": {"codec": "pcm16", "sample_rate": 16000, "channels": 1, "frame_ms": 20,
                      "playback_buffer_ms": 1200},
        }))
        self.hello = json.loads(await asyncio.wait_for(self.ws.recv(), 5))
        self._rx = asyncio.create_task(self._recv_loop())

    async def close(self) -> None:
        if self._rx:
            self._rx.cancel()
        if self.ws:
            await self.ws.close()

    async def _recv_loop(self) -> None:
        import websockets

        try:
            await self._recv_inner()
        except websockets.ConnectionClosed:
            await self.messages.put({"type": "_closed", "_t": time.monotonic()})

    async def _recv_inner(self) -> None:
        async for msg in self.ws:
            if isinstance(msg, bytes):
                if self._t_first_audio is None:
                    self._t_first_audio = time.monotonic()
                self.reply_audio += msg
                continue
            m = json.loads(msg)
            m["_t"] = time.monotonic()
            self.log.append(m)
            await self.messages.put(m)
            if not self.auto_playback_done:
                continue
            if m["type"] == "tts" and m.get("state") == "stop":
                self.replies.append(bytes(self.reply_audio))
                self.reply_audio.clear()
                self._t_first_audio = None
                asyncio.create_task(self._ack_playback({"first_audio_to_speaker_ms": 35}))
            elif m["type"] in ("cue", "error"):
                asyncio.create_task(self._ack_playback({"cue": m.get("name") or m.get("cue")}))

    async def _ack_playback(self, extra: dict[str, Any]) -> None:
        import websockets

        await asyncio.sleep(0.05)
        try:
            await self.send_json({"type": "playback_done", **extra})
        except websockets.ConnectionClosed:
            pass

    async def send_json(self, msg: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(msg))

    async def stream(self, pcm: bytes) -> None:
        for i in range(0, len(pcm), FRAME):
            await self.ws.send(pcm[i:i + FRAME])
            await asyncio.sleep(0.02 / self.speed)

    async def wait_for(self, kind: str, pred: Callable[[dict], bool] | None = None,
                       timeout: float = 10) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no {kind!r} message; seen: {[m['type'] for m in self.log[-10:]]}")
            m = await asyncio.wait_for(self.messages.get(), remaining)
            if m["type"] == kind and (pred is None or pred(m)):
                return m

    async def ptt_turn(self, utterance: bytes) -> dict[str, Any]:
        await self.send_json({"type": "listen", "state": "start", "mode": "ptt"})
        await self.wait_for("state", lambda m: m["state"] == "listening")
        t0 = time.monotonic()
        await self.stream(utterance)
        await self.send_json({"type": "listen", "state": "stop"})
        return {"t_speech_end": time.monotonic(), "t0": t0}


async def _run_cli(a) -> None:
    utterance = speech_like() + silence(1000)
    if a.wav:
        pcm, rate = from_wav(Path(a.wav).read_bytes())
        utterance = resample(pcm, rate) + silence(1000)
    wake = wake_tone()
    if a.wake_wav:
        pcm, rate = from_wav(Path(a.wake_wav).read_bytes())
        wake = resample(pcm, rate)
    dev = SimDevice(a.url, a.device, a.secret, wake="ptt" if a.mode == "ptt" else "cloud")
    await dev.connect()
    print("hello:", json.dumps(dev.hello))
    out = Path(a.save_replies) if a.save_replies else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    ok = 0
    for i in range(a.cycles):
        t_end = None
        try:
            if a.mode == "ptt":
                t_end = (await dev.ptt_turn(utterance[:-32000]))["t_speech_end"]
            else:
                await dev.stream(silence(300) + wake)
                await dev.wait_for("wake", timeout=5)
                await dev.stream(utterance)
                t_end = time.monotonic() - 0.8  # server VAD needs ~800 ms of trailing silence
            stt = await dev.wait_for("stt", timeout=15)
            first_audio_wait = time.monotonic()
            await dev.wait_for("tts", lambda m: m.get("state") == "stop", timeout=30)
            await dev.wait_for("state", lambda m: m["state"] in ("idle", "listening"), timeout=15)
            reply = dev.replies[-1] if dev.replies else b""
            print(f"[{i + 1:02d}] heard={stt['text']!r} reply_audio={len(reply) / 32000:.2f}s "
                  f"end->stt={(stt['_t'] - t_end) * 1000:.0f}ms")
            if out and reply:
                (out / f"reply_{i + 1:02d}.wav").write_bytes(to_wav(reply))
            ok += 1
            del first_audio_wait
            if a.mode != "ptt":
                await dev.stream(silence(int(a.gap_s * 1000)))
            else:
                await asyncio.sleep(a.gap_s)
        except Exception as e:  # noqa: BLE001
            print(f"[{i + 1:02d}] FAILED: {e!r}")
    await dev.close()
    print(f"{ok}/{a.cycles} cycles completed without restart")


def main() -> None:
    ap = argparse.ArgumentParser(prog="mitra-sim")
    ap.add_argument("--url", default="ws://localhost:8080/v1/device/ws")
    ap.add_argument("--device", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--mode", choices=["ptt", "cloud"], default="ptt")
    ap.add_argument("--cycles", type=int, default=1)
    ap.add_argument("--gap-s", type=float, default=5.0)
    ap.add_argument("--wav", help="16-bit WAV to use as the user's question")
    ap.add_argument("--wake-wav", help="16-bit WAV of 'Hey Smith' (cloud mode with openWakeWord)")
    ap.add_argument("--save-replies", help="directory to write reply WAVs")
    asyncio.run(_run_cli(ap.parse_args()))


if __name__ == "__main__":
    main()
