"""One live conversation session per connected main board (PRD §4.3).

Phases (mirrored on the device's OLED, FR-1.2):
  idle        waiting for wake. Cloud-wake mode: uplink audio goes to the wake detector in RAM
              only and is then dropped (FR-2.4).
  listening   audio feeds STT + end-of-utterance detection.
  processing  transcript -> (optional camera) -> LLM.
  responding  TTS audio is streaming / playing. Uplink audio is ignored (FR-3.2 mic gating),
              until the device reports playback_done (it adds its own tail on top).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

from ..audio.pcm import bytes_to_ms, rms_dbfs
from ..audio.vad import FRAME_BYTES, EndpointDetector
from ..observability import (AUDIO_IN_SECONDS, PROVIDER_ERRORS, STAGE_LATENCY, STT_SECONDS, TTS_CHARS,
                             TURNS, WAKE_EVENTS)
from ..providers.base import ChatMessage, ProviderError
from ..repo import Device
from .text import SentenceChunker

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
SEND_CHUNK = 3200          # 100 ms of pcm16 per WebSocket binary frame
WAKE_TAIL_SKIP_MS = 300
STALL_S = 1.5              # listening but no audio arriving -> treat as end of speech


def ms_since(t: float | None) -> float | None:
    return None if t is None else round((time.monotonic() - t) * 1000, 1)


def span(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round((b - a) * 1000, 1)


class Turn:
    """Audio plumbing for one user utterance."""

    def __init__(self, mode: str, end_silence_ms: int, max_utterance_s: float, skip_ms: float = 0,
                 noise_db: float | None = None):
        self.mode = mode                       # wake | ptt | follow_up | repeat | converse
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.vad = EndpointDetector(end_silence_ms=end_silence_ms, noise_db=noise_db)
        self.speech_started = asyncio.Event()
        self.ended = asyncio.Event()
        self.end_reason = ""
        self.max_audio_ms = max_utterance_s * 1000
        self.audio_ms = 0.0
        self.t_start = time.monotonic()
        self.t_last_audio = time.monotonic()
        self.t_speech_end: float | None = None
        # After a cloud wake, drop the tail of the wake phrase (and the device's chime window)
        # so "...Smith" isn't transcribed as the start of the question.
        self.skip_bytes = int(skip_ms * 32)

    def feed(self, chunk: bytes) -> None:
        if self.ended.is_set():
            return
        if self.skip_bytes > 0:
            dropped = min(self.skip_bytes, len(chunk))
            self.skip_bytes -= dropped
            chunk = chunk[dropped:]
            self.t_last_audio = time.monotonic()
            if not chunk:
                return
        self.queue.put_nowait(chunk)
        self.audio_ms += bytes_to_ms(len(chunk))
        self.t_last_audio = time.monotonic()
        for ev in self.vad.feed(chunk):
            if ev.kind == "speech_start":
                self.speech_started.set()
            elif ev.kind == "speech_end" and self.mode != "ptt":
                self.end("vad")
        if self.audio_ms >= self.max_audio_ms:
            self.end("max_utterance")

    def end(self, reason: str) -> None:
        if self.ended.is_set():
            return
        self.end_reason = reason
        self.t_speech_end = time.monotonic()
        self.ended.set()
        self.queue.put_nowait(None)

    async def audio(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                return
            yield chunk


class Pacer:
    """Streams TTS audio no more than ``lead_ms`` ahead of real time so a RAM-starved ESP32's
    jitter buffer never overflows."""

    def __init__(self, send: Callable[[bytes], Awaitable[None]], lead_ms: float):
        self.send = send
        self.lead_ms = lead_ms
        self.t0: float | None = None
        self.sent_ms = 0.0

    async def push(self, pcm: bytes) -> None:
        for i in range(0, len(pcm), SEND_CHUNK):
            piece = pcm[i:i + SEND_CHUNK]
            if self.t0 is None:
                self.t0 = time.monotonic()
            ahead = self.sent_ms - (time.monotonic() - self.t0) * 1000
            if ahead > self.lead_ms:
                await asyncio.sleep((ahead - self.lead_ms) / 1000)
            await self.send(piece)
            self.sent_ms += bytes_to_ms(len(piece))


class DeviceSession:
    def __init__(self, rt, device: Device, hello: dict[str, Any],
                 send_text: Callable[[str], Awaitable[None]], send_bytes: Callable[[bytes], Awaitable[None]],
                 close_ws: Callable[[int, str], Awaitable[None]]):
        import json as _json

        self._json = _json
        self.rt = rt
        self.cfg = rt.cfg
        self.device = device
        self.session_id = uuid.uuid4().hex[:12]
        self._send_text, self._send_bytes, self._close_ws = send_text, send_bytes, close_ws
        self._lock = asyncio.Lock()

        audio = hello.get("audio") or {}
        if audio.get("codec", "pcm16") != "pcm16" or int(audio.get("sample_rate", 16000)) != 16000:
            raise ValueError("only pcm16 @ 16 kHz uplink is supported in protocol v1")
        self.wake_mode = hello.get("wake", "cloud")  # cloud | device | ptt
        # Dev boards / the PC client may carry their own camera; captures then go over this socket.
        self.has_camera = bool(hello.get("camera"))
        # Half the device buffer: the device starts playing only after network delay plus its
        # prebuffer, so 0.75 overflowed it on a laggy phone hotspot ("jitter buffer full").
        self.playback_lead_ms = float(audio.get("playback_buffer_ms", 1200)) * 0.5
        self.language = self.cfg.get("persona.language", "en-IN")
        self.detector = rt.p.wake.create() if (self.wake_mode == "cloud" and rt.p.wake) else None
        # Hands-free clients (e.g. the PC client): greet on connect, then keep listening after
        # every reply without needing the wake word or a button.
        self.greet = bool(hello.get("greet"))
        self.hands_free = bool(hello.get("hands_free"))
        # always_listen: a hands-free window that hears nothing reopens instead of going idle.
        self.always_listen = self.hands_free and bool(hello.get("always_listen"))
        self.greeting = str(self.cfg.get("persona.greeting", "") or "").strip()

        c = self.cfg.section("conversation")
        self.no_speech_s = float(c.get("no_speech_timeout_s", 6))
        self.end_silence_ms = int(c.get("end_of_speech_silence_ms", 800))
        self.max_utt_s = float(c.get("max_utterance_s", 15))
        self.max_repeats = int(c.get("max_repeat_prompts", 2))
        self.min_conf = float(c.get("min_transcript_confidence", 0.45))
        self.follow_up_s = float(c.get("follow_up_window_s", 0))
        self.hands_free_wait_s = float(c.get("hands_free_wait_s", 10))
        t = c.get("timeouts", {}) or {}
        self.stt_s, self.llm_s, self.tts_s = float(t.get("stt_s", 8)), float(t.get("llm_s", 10)), float(t.get("tts_s", 8))
        self.persist = bool(self.cfg.get("conversation.history.persist", False)) and device.history_opt_in
        self.retention_days = int(self.cfg.get("conversation.history.retention_days", 7))
        self.max_sentences = int(self.cfg.get("persona.max_reply_sentences", 4))
        self.vision_intents = [re.compile(p, re.I) for p in (self.cfg.get("vision.intents") or [])]

        self.phase = "idle"
        self.muted = False
        self.turn: Turn | None = None
        self.turn_task: asyncio.Task | None = None
        self.repeats = 0
        self.after_playback = "idle"
        self.playback_guard: asyncio.Task | None = None
        self.pending_metrics: dict[str, Any] | None = None
        self.turn_no = 0
        self.closed = False

    # -- transport ------------------------------------------------------------------------------
    async def send_json(self, msg: dict[str, Any]) -> None:
        if self.closed:
            return
        async with self._lock:
            with contextlib.suppress(Exception):
                await self._send_text(self._json.dumps(msg))

    async def send_audio(self, pcm: bytes) -> None:
        if self.closed:
            raise ConnectionError("session closed")
        async with self._lock:
            await self._send_bytes(pcm)

    async def set_phase(self, phase: str, **extra: Any) -> None:
        self.phase = phase
        await self.send_json({"type": "state", "state": phase, **extra})

    # -- lifecycle ------------------------------------------------------------------------------
    async def start(self) -> None:
        if self.persist:
            past = await self.rt.repo.recent_turns(self.device.id, self.rt.context.max_messages)
            self.rt.context.seed(self.device.id, [ChatMessage(r, c) for r, c in past])
        await self.send_json({
            "type": "hello", "version": PROTOCOL_VERSION, "session_id": self.session_id,
            "audio": {"codec": "pcm16", "sample_rate": 16000, "channels": 1},
            "wake": "cloud" if self.detector else self.wake_mode if self.wake_mode != "cloud" else "ptt",
            "config": {
                "no_speech_timeout_s": self.no_speech_s,
                "follow_up_window_s": self.follow_up_s,
                "playback_tail_ms": int(self.cfg.get("conversation.playback_tail_ms", 350)),
                **self.device.config,
            },
        })
        await self.set_phase("idle")
        if self.greet and self.greeting:
            self.turn_task = asyncio.create_task(self._greet(), name=f"greet-{self.device.id}")
        elif self.hands_free:
            await self._start_turn("converse")

    async def _greet(self) -> None:
        """Speak the configured greeting, then open a hands-free listening turn."""
        then = "converse" if self.hands_free else "idle"
        try:
            audio_ms = await self._speak(self.greeting)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  (TTS down: just start listening)
            log.warning("greeting failed", extra={"device": self.device.id, "err": repr(e)})
            self.turn_task = None
            if self.hands_free:
                await self._start_turn("converse")
            return
        self.rt.context.get(self.device.id).append(ChatMessage("assistant", self.greeting))
        self.turn_task = None
        await self._await_playback(then, audio_ms)
        await self.send_json({"type": "tts", "state": "stop"})

    async def _speak(self, text: str) -> float:
        """Stream a fixed line of speech to the device. Returns ms of audio sent."""
        pacer = Pacer(self.send_audio, self.playback_lead_ms)
        await self.send_json({"type": "tts", "state": "start"})
        await self.set_phase("responding")
        await self.send_json({"type": "tts", "state": "sentence", "text": text})
        log.info("speaking: %r", text, extra={"device": self.device.id})
        TTS_CHARS.labels(self.rt.p.tts.providers[0].name).inc(len(text))

        async def run() -> None:
            async for chunk in self.rt.p.tts.synthesize(text, self.language):
                await pacer.push(chunk)

        await asyncio.wait_for(run(), self.tts_s * 2)
        return pacer.sent_ms

    async def close(self, reason: str) -> None:
        if self.closed:
            return
        kw = getattr(self, "_kw_task", None)
        if kw is not None and not kw.done():
            kw.cancel()
        await self._cancel_turn()
        self._cancel_guard()
        await self._flush_metrics()
        await self.send_json({"type": "goodbye", "reason": reason})
        self.closed = True
        with contextlib.suppress(Exception):
            await self._close_ws(1000, reason)

    # -- uplink -------------------------------------------------------------------------------
    def _track_room_noise(self, chunk: bytes) -> None:
        """Estimate background noise from idle uplink (follows quiet frames fast, loud ones slowly),
        so each turn's end-of-speech detector starts from the real room level."""
        for i in range(0, len(chunk) - FRAME_BYTES + 1, FRAME_BYTES):
            level = rms_dbfs(chunk[i:i + FRAME_BYTES])
            est = getattr(self, "room_noise_db", None)
            if est is None:
                self.room_noise_db = level
            elif level < est:
                self.room_noise_db = 0.7 * est + 0.3 * level
            else:
                self.room_noise_db = 0.995 * est + 0.005 * level

    def _log_wake_check(self, chunk: bytes) -> None:
        """Every ~2 s of idle uplink, log loudness and the best wake score (no audio is kept).
        Answers "is my voice reaching the server, and how close is it to triggering?"."""
        import array
        import math

        s = array.array("h", chunk[: len(chunk) // 2 * 2])
        if s:
            self._wk_sq = getattr(self, "_wk_sq", 0.0) + sum(v * v for v in s)
            self._wk_n = getattr(self, "_wk_n", 0) + len(s)
        if getattr(self, "_wk_n", 0) < 32000:   # 2 s at 16 kHz
            return
        rms = math.sqrt(self._wk_sq / self._wk_n)
        db = 20 * math.log10(rms / 32768) if rms > 0 else -99.0
        best = float(getattr(self.detector, "best", 0.0))
        log.info("wake check", extra={"device": self.device.id, "audio_s": round(self._wk_n / 16000, 1),
                                      "level_dbfs": round(db, 1), "best_score": round(best, 3),
                                      "threshold": getattr(self.detector, "threshold", None)})
        self._wk_sq, self._wk_n = 0.0, 0
        if hasattr(self.detector, "best"):
            self.detector.best = 0.0

    async def _check_wake_keyword(self, seg: bytes) -> None:
        """STT wake: transcribe one idle speech segment; wake if it contains the keyword."""
        async def one() -> AsyncIterator[bytes]:
            yield seg

        try:
            tr = await asyncio.wait_for(self.rt.p.stt.transcribe(one(), self.language), self.stt_s)
        except (asyncio.TimeoutError, ProviderError) as e:
            log.warning("wake stt failed", extra={"device": self.device.id, "err": repr(e)})
            return
        hit = self.detector is not None and self.detector.matches(tr.text)
        log.info("wake check: %r", tr.text, extra={"device": self.device.id, "audio_s": round(len(seg) / 32000, 1),
                                                    "wake": hit})
        if hit and self.phase == "idle":
            await self._on_wake("stt", max(0.5, float(tr.confidence)))

    async def on_audio(self, chunk: bytes) -> None:
        if self.muted or not chunk:
            return
        AUDIO_IN_SECONDS.labels(self.phase).inc(len(chunk) / 32000)
        if self.phase == "idle":
            self._track_room_noise(chunk)
            if self.detector is not None and getattr(self.detector, "needs_stt", False):
                for seg in self.detector.feed(chunk):   # RAM only; dropped after transcription
                    busy = getattr(self, "_kw_task", None)
                    if busy is None or busy.done():     # one at a time; skip if still busy
                        self._kw_task = asyncio.create_task(self._check_wake_keyword(seg))
            elif self.detector is not None:
                score = self.detector.process(chunk)  # RAM only; chunk is dropped after this
                self._log_wake_check(chunk)
                if score is not None:
                    await self._on_wake("cloud", score)
            return
        if self.phase == "listening" and self.turn is not None:
            self.turn.feed(chunk)
        # processing/responding: gated, dropped (FR-3.2)

    async def on_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "listen":
            state = msg.get("state")
            if state == "start":
                mode = msg.get("mode", "ptt")
                if mode == "device_wake":
                    if self.phase != "idle":
                        return
                    await self._on_wake("device", float(msg.get("confidence", 1.0)))
                else:
                    await self._interrupt()
                    self.repeats = 0
                    await self._start_turn("ptt")
            elif state == "stop" and self.turn is not None and self.phase == "listening":
                self.turn.end("ptt_release")
        elif kind == "playback_done":
            await self._on_playback_done(msg)
        elif kind == "abort":
            await self._interrupt()
            await self.set_phase("idle")
        elif kind == "mute":
            self.muted = bool(msg.get("state"))
            if self.muted:
                await self._interrupt()
                await self.set_phase("idle")
        elif kind == "capture":
            # Built-in camera (e.g. PC webcam) reporting a failed capture: fail the job now.
            if msg.get("status") == "error" and msg.get("job_id"):
                self.rt.camera.fail(str(msg["job_id"]), str(msg.get("err", "camera error")))
        elif kind == "ping":
            await self.send_json({"type": "pong", "t": msg.get("t")})
        elif kind == "status":
            # Main board "reported" shadow travels over the WebSocket (one TLS session saves RAM
            # on a PSRAM-less ESP32); cameras report over MQTT instead.
            await self._update_reported({k: v for k, v in msg.items() if k != "type"})

    async def _update_reported(self, fields: dict[str, Any]) -> None:
        from ..repo import now

        dev = await self.rt.repo.get_device(self.device.id)
        if dev is None:
            return
        reported = {**dev.reported, **fields, "updated_at": now().isoformat()}
        await self.rt.repo.update_device(self.device.id, reported=reported, last_seen=now(),
                                         fw_version=fields.get("fw", dev.fw_version))

    # -- turn orchestration ---------------------------------------------------------------------
    async def _on_wake(self, engine: str, confidence: float) -> None:
        if self.phase != "idle":
            return
        WAKE_EVENTS.labels(engine).inc()
        aid = await self.rt.repo.add_activation(self.device.id, engine, confidence)
        log.info("wake", extra={"device": self.device.id, "engine": engine, "conf": round(confidence, 3),
                                "activation_id": aid})
        if self.detector:
            self.detector.reset()
        self.repeats = 0
        await self.send_json({"type": "wake", "engine": engine, "confidence": round(confidence, 3),
                              "activation_id": aid})
        await self._start_turn("wake")

    async def _start_turn(self, mode: str) -> None:
        await self._cancel_turn()
        self._cancel_guard()
        await self._flush_metrics()
        self.turn = Turn(mode, self.end_silence_ms, self.max_utt_s,
                         skip_ms=WAKE_TAIL_SKIP_MS if mode == "wake" else 0,
                         noise_db=getattr(self, "room_noise_db", None))
        self.turn_no += 1
        await self.set_phase("listening", mode=mode)
        self.turn_task = asyncio.create_task(self._run_turn(self.turn), name=f"turn-{self.device.id}")

    async def _interrupt(self) -> None:
        await self._cancel_turn()
        self._cancel_guard()
        await self._flush_metrics()

    async def _cancel_turn(self) -> None:
        task, self.turn_task = self.turn_task, None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.turn = None

    def _cancel_guard(self) -> None:
        if self.playback_guard and not self.playback_guard.done():
            self.playback_guard.cancel()
        self.playback_guard = None

    async def _await_playback(self, then: str, expected_ms: float) -> None:
        """Hand the speaker to the device; next phase is decided when it reports playback_done."""
        self.after_playback = then
        self.phase = "responding"
        self._cancel_guard()

        async def guard() -> None:
            await asyncio.sleep(expected_ms / 1000 + 5)
            log.warning("playback_done not received; continuing", extra={"device": self.device.id})
            self.playback_guard = None  # don't let the next phase cancel this very task
            await self._on_playback_done({"guard": True})

        self.playback_guard = asyncio.create_task(guard())

    async def _on_playback_done(self, msg: dict[str, Any]) -> None:
        if self.phase != "responding":
            return
        self._cancel_guard()
        if self.pending_metrics is not None and msg.get("first_audio_to_speaker_ms") is not None:
            self.pending_metrics["first_audio_to_speaker_ms"] = float(msg["first_audio_to_speaker_ms"])
            if self.pending_metrics.get("end_to_first_audio_ms") is not None:
                self.pending_metrics["end_to_first_audio_ms"] += float(msg["first_audio_to_speaker_ms"])
        if msg.get("underrun"):
            log.warning("device playback underrun", extra={"device": self.device.id})
        await self._flush_metrics()
        nxt = self.after_playback
        if nxt == "listen":
            await self._start_turn("repeat")
        elif nxt == "converse":
            await self._start_turn("converse")
        elif nxt == "follow_up" and self.follow_up_s > 0:
            await self._start_turn("follow_up")
        else:
            await self.set_phase("idle")

    @property
    def _after_error(self) -> str:
        """Hands-free clients go back to listening after a failed turn so the user can just retry."""
        return "converse" if self.hands_free else "idle"

    async def _cue(self, name: str, then: str, code: str | None = None) -> None:
        msg: dict[str, Any] = {"type": "cue", "name": name}
        if code:
            msg = {"type": "error", "code": code, "cue": name}
        # Arm the playback handoff *before* sending, so a fast playback_done can't race past it.
        await self._await_playback(then, expected_ms=3000)
        await self.send_json(msg)

    async def _flush_metrics(self) -> None:
        m, self.pending_metrics = self.pending_metrics, None
        if not m:
            return
        for k in ("speech_end_to_transcript_ms", "transcript_to_first_token_ms",
                  "first_token_to_first_audio_ms", "end_to_first_audio_ms", "first_audio_to_speaker_ms"):
            if m.get(k) is not None:
                STAGE_LATENCY.labels(k.removesuffix("_ms")).observe(m[k])
        TURNS.labels(m.get("outcome", "unknown")).inc()
        log.info("turn", extra={"device": self.device.id, **{k: v for k, v in m.items() if k != "device_id"}})
        try:
            await self.rt.repo.add_metrics(m)
        except Exception:  # noqa: BLE001
            log.exception("failed to store metrics")

    async def _run_turn(self, turn: Turn) -> None:
        m: dict[str, Any] = {
            "device_id": self.device.id, "session_id": self.session_id, "turn": self.turn_no,
            "mode": turn.mode, "stt": self.rt.p.stt.name, "llm": self.rt.p.llm.name, "outcome": "unknown",
        }
        self.pending_metrics = m
        stt_task = asyncio.create_task(self.rt.p.stt.transcribe(turn.audio(), self.language))
        try:
            # 1. wait for speech to start (FR-1.3) ---------------------------------------------
            no_speech_s = {"follow_up": self.follow_up_s,
                           "converse": self.hands_free_wait_s}.get(turn.mode, self.no_speech_s)
            waiters = [asyncio.create_task(turn.speech_started.wait()), asyncio.create_task(turn.ended.wait())]
            await asyncio.wait(waiters, timeout=no_speech_s, return_when=asyncio.FIRST_COMPLETED)
            for w in waiters:
                w.cancel()
            if not turn.speech_started.is_set() and not (turn.mode == "ptt" and turn.ended.is_set()):
                turn.end("no_speech")
                stt_task.cancel()
                m["outcome"] = "no_speech"
                # Follow-up windows close silently; a real wake that hears nothing plays the cue.
                if turn.mode == "converse" and self.always_listen:
                    # Devices that ask for it (e.g. a board without a working button) keep
                    # listening: open the next window silently instead of going idle for good.
                    self.pending_metrics = None
                    await self._start_turn("converse")
                elif turn.mode in ("follow_up", "converse"):
                    self.pending_metrics = None   # an unused follow-up window isn't a turn
                    await self.set_phase("idle")
                else:
                    await self._cue("standby", then="idle")
                return

            # 2. wait for end of utterance -----------------------------------------------------
            hard_stop = time.monotonic() + self.max_utt_s + 2
            while not turn.ended.is_set():
                try:
                    await asyncio.wait_for(turn.ended.wait(), 0.2)
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() - turn.t_last_audio > STALL_S:
                    turn.end("stall")
                elif time.monotonic() > hard_stop:
                    turn.end("max_utterance")
            await self.set_phase("processing")
            m["end_reason"] = turn.end_reason
            m["audio_in_ms"] = round(turn.audio_ms)
            STT_SECONDS.labels(self.rt.p.stt.name).inc(turn.audio_ms / 1000)

            # 3. transcript ---------------------------------------------------------------------
            try:
                tr = await asyncio.wait_for(stt_task, self.stt_s)
            except (asyncio.TimeoutError, ProviderError) as e:
                PROVIDER_ERRORS.labels("stt", self.rt.p.stt.name).inc()
                log.warning("stt failed", extra={"err": repr(e)})
                m["outcome"] = "stt_error"
                await self._cue("no_brain", then=self._after_error, code="stt_timeout" if isinstance(e, asyncio.TimeoutError) else "stt_error")
                return
            t_transcript = time.monotonic()
            m["speech_end_to_transcript_ms"] = span(turn.t_speech_end, t_transcript)
            m["stt_confidence"] = round(tr.confidence, 3)
            log.info("heard: %r", tr.text, extra={"device": self.device.id, "conf": round(tr.confidence, 3),
                                                  "audio_ms": round(turn.audio_ms), "end": turn.end_reason})

            if not tr.text.strip() or tr.confidence < self.min_conf:            # FR-1.4
                self.repeats += 1
                m["outcome"] = "unclear"
                if self.repeats <= self.max_repeats:
                    await self._cue("repeat", then="listen")
                else:
                    self.repeats = 0
                    await self._cue("standby", then="idle")
                return
            self.repeats = 0
            await self.send_json({"type": "stt", "text": tr.text})

            # 4. optional camera (FR-5.6) ------------------------------------------------------
            user_content = tr.text
            if self.rt.camera.available and any(p.search(tr.text) for p in self.vision_intents):
                outcome = await self.rt.camera.capture_and_describe(self.device.id, tr.text)
                m["vision"] = outcome.reason or "ok"
                m["capture_ms"] = outcome.capture_ms and round(outcome.capture_ms)
                m["vision_ms"] = outcome.vision_ms and round(outcome.vision_ms)
                if outcome.ok and outcome.result:
                    obs = outcome.result.description
                elif outcome.reason == "low_confidence":
                    obs = "The view was unclear (dark, blurry or too far). You are not sure what you see."  # FR-5.2
                else:
                    obs = "Your camera is not available right now, so you cannot see anything."
                user_content = f"{tr.text}\n[Camera observation] {obs}"

            # 5+6. LLM -> sentences -> TTS -> device, all streaming --------------------------
            reply, audio_ms, t_first_token, t_first_audio = await self._respond(user_content, m)
            log.info("reply: %r", reply, extra={"device": self.device.id, "audio_out_ms": round(audio_ms)})
            m["transcript_to_first_token_ms"] = span(t_transcript, t_first_token)
            m["first_token_to_first_audio_ms"] = span(t_first_token, t_first_audio)
            m["end_to_first_audio_ms"] = span(turn.t_speech_end, t_first_audio)
            m["reply_chars"] = len(reply)
            m["audio_out_ms"] = round(audio_ms)

            ctx = self.rt.context.get(self.device.id)
            ctx.append(ChatMessage("user", user_content))
            ctx.append(ChatMessage("assistant", reply))
            if self.persist:
                await self.rt.repo.add_turn(self.device.id, self.session_id, "user", user_content, self.retention_days)
                await self.rt.repo.add_turn(self.device.id, self.session_id, "assistant", reply, self.retention_days)
            m["outcome"] = "ok"
            nxt = "converse" if self.hands_free else "follow_up" if self.follow_up_s > 0 else "idle"
            await self._await_playback(nxt, audio_ms)
            await self.send_json({"type": "tts", "state": "stop"})
        except asyncio.CancelledError:
            m["outcome"] = m.get("outcome") if m.get("outcome") != "unknown" else "cancelled"
            stt_task.cancel()
            raise
        except _ResponseFailed as e:
            m["outcome"] = e.code
            if e.audio_sent:
                await self._await_playback(self._after_error, e.audio_sent)
                await self.send_json({"type": "tts", "state": "stop", "reason": e.code})
            else:
                await self._cue("no_brain", then=self._after_error, code=e.code)
        except Exception:  # noqa: BLE001
            log.exception("turn crashed", extra={"device": self.device.id})
            m["outcome"] = "internal_error"
            await self._cue("no_brain", then=self._after_error, code="internal_error")
        finally:
            if not stt_task.done():
                stt_task.cancel()

    async def _respond(self, user_content: str, m: dict[str, Any]) -> tuple[str, float, float | None, float | None]:
        rt = self.rt
        ctx = list(rt.context.get(self.device.id))
        messages = ctx + [ChatMessage("user", user_content)]
        system = self.cfg.system_prompt()
        q: asyncio.Queue[str | None] = asyncio.Queue()
        reply_parts: list[str] = []
        state: dict[str, Any] = {"t_first_token": None, "t_first_audio": None, "started": False}
        pacer = Pacer(self.send_audio, self.playback_lead_ms)
        loop = asyncio.get_running_loop()

        async def produce() -> None:
            chunker = SentenceChunker()
            n_sent = 0
            agen = rt.p.llm.stream(system, messages).__aiter__()
            deadline = loop.time() + self.llm_s * 2
            first = True
            while True:
                timeout = self.llm_s if first else max(0.1, deadline - loop.time())
                try:
                    delta = await asyncio.wait_for(agen.__anext__(), timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise _ResponseFailed("llm_timeout", pacer.sent_ms) from None
                except ProviderError:
                    PROVIDER_ERRORS.labels("llm", rt.p.llm.name).inc()
                    raise _ResponseFailed("llm_error", pacer.sent_ms) from None
                if first:
                    state["t_first_token"] = time.monotonic()
                    first = False
                reply_parts.append(delta)
                for s in chunker.feed(delta):
                    if n_sent < self.max_sentences:
                        await q.put(s)
                        n_sent += 1
            for s in chunker.flush():
                if n_sent < self.max_sentences:
                    await q.put(s)
                    n_sent += 1
            await q.put(None)

        async def consume() -> None:
            while (sentence := await q.get()) is not None:
                if not state["started"]:
                    state["started"] = True
                    await self.send_json({"type": "tts", "state": "start"})
                    await self.set_phase("responding")
                await self.send_json({"type": "tts", "state": "sentence", "text": sentence})
                log.info("speaking: %r", sentence, extra={"device": self.device.id})
                TTS_CHARS.labels(rt.p.tts.providers[0].name).inc(len(sentence))
                gen = rt.p.tts.synthesize(sentence, self.language).__aiter__()
                sent_deadline = loop.time() + self.tts_s * 2
                first = True
                while True:
                    timeout = self.tts_s if first else max(0.1, sent_deadline - loop.time())
                    try:
                        chunk = await asyncio.wait_for(gen.__anext__(), timeout)
                    except StopAsyncIteration:
                        break
                    except (asyncio.TimeoutError, ProviderError) as e:
                        PROVIDER_ERRORS.labels("tts", rt.p.tts.name).inc()
                        code = "tts_timeout" if isinstance(e, asyncio.TimeoutError) else "tts_error"
                        raise _ResponseFailed(code, pacer.sent_ms) from None
                    first = False
                    if state["t_first_audio"] is None:
                        state["t_first_audio"] = time.monotonic()
                    try:
                        await pacer.push(chunk)
                    except Exception:  # noqa: BLE001  (socket gone)
                        raise _ResponseFailed("device_gone", pacer.sent_ms) from None
            # "tts stop" is sent by the caller after bookkeeping (see _run_turn).
            m["tts"] = rt.p.tts.last_used

        prod = asyncio.create_task(produce())
        cons = asyncio.create_task(consume())
        try:
            done, _ = await asyncio.wait({prod, cons}, return_when=asyncio.FIRST_EXCEPTION)
            for t in done:
                if t.exception():
                    raise t.exception()  # type: ignore[misc]
            await asyncio.gather(prod, cons)
        finally:
            for t in (prod, cons):
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t
        reply = "".join(reply_parts).strip()
        if not reply:
            raise _ResponseFailed("llm_empty", pacer.sent_ms)
        return reply, pacer.sent_ms, state["t_first_token"], state["t_first_audio"]


class _ResponseFailed(Exception):
    def __init__(self, code: str, audio_sent: float):
        super().__init__(code)
        self.code = code
        self.audio_sent = audio_sent
