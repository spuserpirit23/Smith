"""Run Mitra on a PC: laptop mic + speaker stand in for the ESP32 main board, the webcam stands in
for the ESP32-CAM. Speaks protocol v1 (docs/PROTOCOL.md), like ``mitra.sim`` but with real audio.

  pip install -e ".[pc]"
  mitra-pc check                      # 3 s mic record -> playback, one webcam photo; no backend needed
  mitra-pc devices                    # list audio devices (use the index with --mic / --speaker)
  mitra-pc run                        # connect to the local backend (auto-provisions device "pc-001")

By default Mitra greets you on connect and keeps listening after each reply (hands-free).
Use --manual for pure push-to-talk.

Keys while running:  Enter = start/stop talking (push-to-talk)   l = look (camera test)
                     s = snapshot to disk                        q = quit
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from .audio.pcm import resample, rms_dbfs, to_wav, tone

RATE = 16000
FRAME_SAMPLES = 320          # 20 ms uplink frames
PLAYBACK_BUFFER_MS = 1200


def _need(mod: str):
    try:
        return __import__(mod)
    except ImportError:
        sys.exit(f"missing dependency '{mod}'. Install with:  pip install -e \".[pc]\"")


# ---------------------------------------------------------------------------------------- audio
class Mic:
    """16 kHz mono int16 capture. Falls back to the device's native rate + resampling when the
    host API refuses 16 kHz (WASAPI does)."""

    def __init__(self, device: int | None, on_frame):
        sd = _need("sounddevice")
        self.on_frame = on_frame
        self.level_dbfs = -120.0
        try:
            self.stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", device=device,
                                            blocksize=FRAME_SAMPLES, callback=self._cb16)
            self.src_rate = RATE
        except Exception:  # noqa: BLE001
            info = sd.query_devices(device, "input")
            self.src_rate = int(info["default_samplerate"])
            self.stream = sd.RawInputStream(samplerate=self.src_rate, channels=1, dtype="int16", device=device,
                                            blocksize=int(self.src_rate * 0.02), callback=self._cb_native)
        self.stream.start()

    def _cb16(self, data, frames, t, status) -> None:
        pcm = bytes(data)
        self.level_dbfs = rms_dbfs(pcm)
        self.on_frame(pcm)

    def _cb_native(self, data, frames, t, status) -> None:
        self._cb16(resample(bytes(data), self.src_rate), frames, t, status)

    def close(self) -> None:
        self.stream.stop()
        self.stream.close()


class Speaker:
    """Jitter buffer + output stream. ``drained`` tells the protocol layer when playback ended."""

    def __init__(self, device: int | None):
        sd = _need("sounddevice")
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.t_first_out: float | None = None
        try:
            self.out_rate = RATE
            self.stream = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", device=device,
                                             blocksize=320, callback=self._cb)
        except Exception:  # noqa: BLE001
            info = sd.query_devices(device, "output")
            self.out_rate = int(info["default_samplerate"])
            self.stream = sd.RawOutputStream(samplerate=self.out_rate, channels=1, dtype="int16",
                                             device=device, blocksize=int(self.out_rate * 0.02),
                                             callback=self._cb)
        self.stream.start()

    def _cb(self, out, frames, t, status) -> None:
        need = frames * 2
        with self._lock:
            chunk = bytes(self._buf[:need])
            del self._buf[:need]
        if chunk and self.t_first_out is None:
            self.t_first_out = time.monotonic()
        out[:len(chunk)] = chunk
        if len(chunk) < need:
            out[len(chunk):need] = b"\x00" * (need - len(chunk))

    def play(self, pcm16_16k: bytes) -> None:
        if self.out_rate != RATE:
            pcm16_16k = resample(pcm16_16k, RATE, self.out_rate)
        with self._lock:
            self._buf += pcm16_16k

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    @property
    def drained(self) -> bool:
        with self._lock:
            return not self._buf

    def close(self) -> None:
        self.stream.stop()
        self.stream.close()


# Local cue sounds (the firmware ships recorded clips; tones are enough on a PC).
CUES = {
    "wake": tone(880, 90, 0.3) + tone(1320, 120, 0.3),
    "repeat": tone(660, 150, 0.3) + b"\x00\x00" * 1600 + tone(660, 150, 0.3),
    "standby": tone(660, 120, 0.3) + tone(440, 200, 0.3),
    "no_brain": tone(300, 400, 0.3),
    "no_wifi": tone(250, 500, 0.3),
}


# --------------------------------------------------------------------------------------- camera
class Webcam:
    def __init__(self, index: int):
        self.index = index
        self._lock = threading.Lock()
        self.last_brightness: float | None = None   # mean pixel 0-255; < 25 = shutter closed / dark room

    @property
    def dark_hint(self) -> str:
        if self.last_brightness is not None and self.last_brightness < 25:
            return f"  WARNING: image is very dark (brightness {self.last_brightness:.0f}/255) - privacy shutter closed or room too dark?"
        return ""

    def snap(self, max_bytes: int = 380_000) -> bytes:
        """Open, let auto-exposure settle, grab one VGA JPEG, close (camera LED is on only while capturing)."""
        cv2 = _need("cv2")
        with self._lock:
            cap = cv2.VideoCapture(self.index, cv2.CAP_MSMF if os.name == "nt" else cv2.CAP_ANY)
            try:
                if not cap.isOpened():
                    raise RuntimeError(f"cannot open webcam #{self.index}")
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                # Auto-exposure warm-up: the first frames after opening are black or blown out.
                # Read for at least 0.6 s, then until brightness is steady and sane (or 3 s cap).
                frame, prev_b, steady = None, None, 0
                t0 = time.monotonic()
                while True:
                    ok, f = cap.read()
                    now = time.monotonic() - t0
                    if not ok:
                        if now > 4:
                            raise RuntimeError("webcam returned no frames (in use by another app?)")
                        continue
                    frame, b = f, float(f.mean())
                    steady = steady + 1 if prev_b is not None and abs(b - prev_b) < 6 else 0
                    prev_b = b
                    if now >= 0.6 and steady >= 3 and 20 < b < 230:
                        break
                    if now >= 3.0:          # still dark/bright: room is dark or shutter closed
                        break
            finally:
                cap.release()
        self.last_brightness = float(frame.mean())
        if frame.shape[1] > 640:
            frame = cv2.resize(frame, (640, int(frame.shape[0] * 640 / frame.shape[1])))
        for q in (85, 70, 55, 40):
            ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, q])
            if ok and len(jpg) <= max_bytes:
                return jpg.tobytes()
        raise RuntimeError("could not encode frame under size limit")


# ---------------------------------------------------------------------------------------- client
class PcDevice:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.ws = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.state = "idle"
        self.wake_mode = "ptt"
        self.tail_ms = 350
        self.uplink: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self.mic_gate_until = 0.0
        self.playing_tts = False
        self.tts_stopped = False
        self.cue_pending: str | None = None
        self.t_first_audio_rx: float | None = None
        self.utterance = bytearray()
        self.save_dir = Path(a.save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.cam = None if a.no_camera else Webcam(a.camera)
        self.mic: Mic | None = None
        self.spk: Speaker | None = None

    # -- helpers ------------------------------------------------------------------------------
    def say(self, msg: str) -> None:
        print(f"\r\x1b[K{msg}", flush=True)

    async def send(self, msg: dict[str, Any]) -> None:
        await self.ws.send(json.dumps(msg))

    def _on_mic(self, pcm: bytes) -> None:      # audio thread
        if self.loop:
            self.loop.call_soon_threadsafe(self._enqueue, pcm)

    def _enqueue(self, pcm: bytes) -> None:
        streaming = self.state == "listening" or (self.state == "idle" and self.wake_mode == "cloud")
        if not streaming or time.monotonic() < self.mic_gate_until or self.playing_tts:
            return  # FR-3.2: mic is gated while speaking + tail
        if self.state == "listening":
            self.utterance += pcm
        if not self.uplink.full():
            self.uplink.put_nowait(pcm)

    async def _uplink_loop(self) -> None:
        while True:
            await self.ws.send(await self.uplink.get())

    async def _meter_loop(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            if self.state == "listening" and self.mic:
                db = max(-60.0, self.mic.level_dbfs)
                bar = "#" * int((db + 60) / 2)
                sys.stdout.write(f"\r\x1b[K  mic {db:6.1f} dBFS |{bar:<30}|  (Enter to stop)")
                sys.stdout.flush()

    # -- playback / handshake with the server ---------------------------------------------------
    async def _wait_drain_then_done(self, extra: dict[str, Any]) -> None:
        while not self.spk.drained:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.12)                                   # device output latency
        self.mic_gate_until = time.monotonic() + self.tail_ms / 1000
        self.playing_tts = False
        await asyncio.sleep(self.tail_ms / 1000)
        await self.send({"type": "playback_done", **extra})

    async def _play_cue(self, name: str) -> None:
        self.playing_tts = True
        self.spk.play(CUES.get(name, CUES["no_brain"]))
        await self._wait_drain_then_done({"cue": name})

    async def _capture(self, cmd: dict[str, Any]) -> None:
        import httpx

        async def report(err: str) -> None:
            self.say(f"  [camera] FAILED: {err}")
            with contextlib.suppress(Exception):
                await self.send({"type": "capture", "job_id": cmd.get("job_id"), "status": "error", "err": err})

        if self.cam is None:
            return await report("camera disabled (--no-camera)")
        self.say("  [camera] capturing...")
        try:
            t0 = time.monotonic()
            jpg = await asyncio.to_thread(self.cam.snap)
            (self.save_dir / "last_capture.jpg").write_bytes(jpg)
            async with httpx.AsyncClient(timeout=10) as h:
                r = await h.post(cmd["upload_url"], content=jpg, headers={"Content-Type": "image/jpeg"})
            if r.status_code >= 400:
                return await report(f"upload rejected: HTTP {r.status_code} {r.text[:120]}")
            self.say(f"  [camera] uploaded {len(jpg) // 1024} KB in {time.monotonic() - t0:.1f}s "
                     f"(saved {self.save_dir / 'last_capture.jpg'})")
            if self.cam.dark_hint:
                self.say(self.cam.dark_hint)
        except Exception as e:  # noqa: BLE001
            await report(str(e) or type(e).__name__)

    async def _recv_loop(self) -> None:
        async for msg in self.ws:
            if isinstance(msg, bytes):
                if self.t_first_audio_rx is None:
                    self.t_first_audio_rx = time.monotonic()
                    self.spk.t_first_out = None
                self.spk.play(msg)
                continue
            m = json.loads(msg)
            t = m.get("type")
            if t == "state":
                prev, self.state = self.state, m["state"]
                if self.state == "listening" and prev != "listening":
                    self.utterance.clear()
                    self.say(f"[{self.state}] {m.get('mode', '')} - speak now")
                elif self.state != "listening":
                    if prev == "listening" and self.utterance:
                        wav = self.save_dir / "last_utterance.wav"
                        wav.write_bytes(to_wav(bytes(self.utterance)))
                        self.say(f"  sent {len(self.utterance) / 32000:.1f}s of audio (saved {wav})")
                    self.say(f"[{self.state}]")
                    if self.state == "idle" and not self.a.manual and prev == "listening":
                        self.say("  (went quiet - press Enter to talk again)")
            elif t == "wake":
                self.say(f"  wake word ({m.get('confidence')})")
                self.mic_gate_until = time.monotonic() + 0.35          # don't send our own chime
                self.spk.play(CUES["wake"])
            elif t == "stt":
                self.say(f"  you said: {m['text']!r}")
            elif t == "tts":
                st = m.get("state")
                if st == "start":
                    self.playing_tts, self.t_first_audio_rx = True, None
                elif st == "sentence":
                    self.say(f"  mitra: {m['text']}")
                elif st == "stop":
                    extra: dict[str, Any] = {}
                    if self.t_first_audio_rx and self.spk.t_first_out:
                        extra["first_audio_to_speaker_ms"] = round(
                            (self.spk.t_first_out - self.t_first_audio_rx) * 1000)
                    self.playing_tts = True
                    asyncio.create_task(self._wait_drain_then_done(extra))
            elif t == "cue":
                self.say(f"  cue: {m['name']}")
                asyncio.create_task(self._play_cue(m["name"]))
            elif t == "error":
                self.say(f"  error: {m.get('code')}")
                asyncio.create_task(self._play_cue(m.get("cue", "no_brain")))
            elif t == "capture":
                asyncio.create_task(self._capture(m))
            elif t == "goodbye":
                self.say(f"  server said goodbye: {m.get('reason')}")
                return
            elif t not in ("pong", "camera", "config"):
                self.say(f"  <- {m}")

    async def _keys_loop(self) -> None:
        while True:
            raw = await asyncio.to_thread(sys.stdin.readline)
            line = raw.strip().lower()
            if raw == "" or line == "q":   # EOF or quit
                return
            if line == "l":
                await self._look()
            elif line == "s":
                await self._snapshot()
            elif self.state == "listening":
                await self.send({"type": "listen", "state": "stop"})
            elif self.state in ("idle", "responding", "processing"):
                self.spk.clear()
                self.playing_tts = False
                await self.send({"type": "listen", "state": "start", "mode": "ptt"})

    async def _snapshot(self) -> None:
        if self.cam is None:
            return self.say("camera disabled (--no-camera)")
        try:
            jpg = await asyncio.to_thread(self.cam.snap)
            p = self.save_dir / f"snap_{int(time.time())}.jpg"
            p.write_bytes(jpg)
            self.say(f"  snapshot saved: {p}")
            if self.cam.dark_hint:
                self.say(self.cam.dark_hint)
        except Exception as e:  # noqa: BLE001
            self.say(f"  snapshot failed: {e}")

    async def _look(self) -> None:
        """Ask the backend to run the full capture -> upload -> vision path (admin /look)."""
        import httpx

        token = _admin_token()
        if not token:
            return self.say("  look needs MITRA_ADMIN_TOKEN (env or .env)")
        async with httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {token}"}) as h:
            r = await h.post(f"{self.a.api}/v1/admin/devices/{self.a.device}/look")
        self.say(f"  look -> {r.json() if r.status_code < 400 else r.text}")

    # -- main -----------------------------------------------------------------------------------
    async def run(self) -> None:
        import websockets

        self.loop = asyncio.get_running_loop()
        secret = self.a.secret or await asyncio.to_thread(_provision, self.a.api, self.a.device)
        url = self.a.api.replace("http", "ws", 1) + "/v1/device/ws"
        self.ws = await websockets.connect(url, additional_headers={
            "X-Device-Id": self.a.device, "Authorization": f"Bearer {secret}"}, max_size=2**20)
        await self.send({
            "type": "hello", "version": 1, "fw": "pc-0.1.0", "wake": self.a.mode,
            "camera": self.cam is not None,
            # Greet on connect and keep listening after each reply (no Enter needed).
            "greet": not self.a.manual, "hands_free": not self.a.manual,
            "audio": {"codec": "pcm16", "sample_rate": RATE, "channels": 1, "frame_ms": 20,
                      "playback_buffer_ms": PLAYBACK_BUFFER_MS},
        })
        hello = json.loads(await asyncio.wait_for(self.ws.recv(), 5))
        self.wake_mode = hello.get("wake", "ptt")
        self.tail_ms = int(hello.get("config", {}).get("playback_tail_ms", 350))
        self.say(f"connected to {url} as {self.a.device}  (wake={self.wake_mode}, "
                 f"camera={'webcam #%d' % self.a.camera if self.cam else 'off'})")
        if self.a.mode == "cloud" and self.wake_mode != "cloud":
            self.say("  server has no wake engine (MITRA_WAKE_PROVIDER) -> using push-to-talk")
        self.mic = Mic(self.a.mic, self._on_mic)
        self.spk = Speaker(self.a.speaker)
        if self.a.manual:
            self.say("ready.  Enter = talk / stop    l = look (camera test)    s = snapshot    q = quit\n")
        else:
            self.say("ready.  Mitra will greet you - just talk. It keeps listening after each reply.\n"
                     "        Enter = talk / interrupt    l = look    s = snapshot    q = quit\n")
        tasks = [asyncio.create_task(c) for c in
                 (self._recv_loop(), self._uplink_loop(), self._meter_loop(), self._keys_loop())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            self.mic.close()
            self.spk.close()
            await self.ws.close()


# ------------------------------------------------------------------------------ provisioning
def _dotenv() -> dict[str, str]:
    # Current dir first, then ./backend/.env (run from the repo root), then the backend folder
    # this package lives in - so mitra-pc works no matter which directory it's started from.
    candidates = [Path(".env"), Path("backend/.env"), Path(__file__).resolve().parents[1] / ".env"]
    p = next((c for c in candidates if c.is_file()), None)
    out: dict[str, str] = {}
    if p is not None:
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _admin_token() -> str:
    return os.environ.get("MITRA_ADMIN_TOKEN") or _dotenv().get("MITRA_ADMIN_TOKEN", "")


def _provision(api: str, device: str) -> str:
    """Dev convenience: (re)create the device and return its fresh secret. The in-memory dev DB
    forgets devices on every backend restart, so doing this on each run keeps things one-command."""
    import httpx

    token = _admin_token()
    if not token:
        sys.exit("pass --secret, or set MITRA_ADMIN_TOKEN (env or backend/.env) to auto-provision")
    with httpx.Client(base_url=api, headers={"Authorization": f"Bearer {token}"}, timeout=10) as c:
        try:
            c.delete(f"/v1/admin/devices/{device}")
            r = c.post("/v1/admin/devices", json={"id": device, "kind": "main", "name": "PC test device"})
        except httpx.ConnectError:
            sys.exit(f"backend not reachable at {api}. Start it first (see docs/PC_TESTING.md).")
        if r.status_code >= 400:
            sys.exit(f"provisioning failed: HTTP {r.status_code} {r.text}")
        return r.json()["secret"]


# ---------------------------------------------------------------------------------- self-check
def cmd_check(a: argparse.Namespace) -> None:
    sd = _need("sounddevice")
    out = Path(a.save_dir)
    out.mkdir(parents=True, exist_ok=True)
    print(f"mic:     {sd.query_devices(a.mic, 'input')['name']}")
    print(f"speaker: {sd.query_devices(a.speaker, 'output')['name']}")
    print("Recording 3 s - say something...")
    rec = sd.rec(3 * RATE, samplerate=RATE, channels=1, dtype="int16", device=a.mic)
    sd.wait()
    pcm = rec.astype("<i2").tobytes()
    db = rms_dbfs(pcm)
    peak = int(np.abs(rec).max())
    (out / "check_mic.wav").write_bytes(to_wav(pcm))
    verdict = ("OK" if db > -45 else "QUIET - move closer / raise mic level in Windows Sound settings"
               if db > -65 else "SILENT - wrong device or mic blocked (Settings > Privacy > Microphone)")
    print(f"  level {db:.1f} dBFS, peak {peak}  -> {verdict}")
    print("Playing it back...")
    sd.play(rec, samplerate=RATE, device=a.speaker)
    sd.wait()
    if not a.no_camera:
        print(f"Webcam #{a.camera}: capturing...")
        try:
            cam = Webcam(a.camera)
            jpg = cam.snap()
            (out / "check_camera.jpg").write_bytes(jpg)
            print(f"  OK {len(jpg) // 1024} KB -> {(out / 'check_camera.jpg').resolve()}")
            if cam.dark_hint:
                print(cam.dark_hint)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED: {e}  (try --camera 1, or Settings > Privacy > Camera)")
    print(f"Files in {out.resolve()}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="mitra-pc", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "check", "devices"])
    ap.add_argument("--api", default=os.environ.get("MITRA_API", "http://localhost:8080"))
    ap.add_argument("--device", default="pc-001")
    ap.add_argument("--secret", help="device secret (default: auto-provision with the admin token)")
    ap.add_argument("--mode", choices=["ptt", "cloud"], default="ptt",
                    help="cloud = server-side wake word (needs MITRA_WAKE_PROVIDER=openwakeword)")
    ap.add_argument("--manual", action="store_true",
                    help="no greeting / hands-free listening: press Enter for every turn")
    ap.add_argument("--mic", type=int, help="input device index (see `mitra-pc devices`)")
    ap.add_argument("--speaker", type=int, help="output device index")
    ap.add_argument("--camera", type=int, default=0, help="webcam index")
    ap.add_argument("--no-camera", action="store_true")
    ap.add_argument("--save-dir", default=os.path.join(
        os.environ.get("MITRA_DATA_DIR") or _dotenv().get("MITRA_DATA_DIR") or ".", "pc"))
    a = ap.parse_args()
    if a.cmd == "devices":
        print(_need("sounddevice").query_devices())
        return
    if a.cmd == "check":
        cmd_check(a)
        return
    try:
        asyncio.run(PcDevice(a).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
