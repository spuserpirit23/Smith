"""Camera orchestration (PRD §5.5, option A/B).

Flow: backend picks the paired ESP32-CAM -> publishes a one-shot ``capture`` command over MQTT
containing a signed upload URL -> camera POSTs a single JPEG to that URL -> frame is analysed in
memory and discarded (FR-5.5) unless the operator turned on debug retention.

Compared with the PRD's pre-signed S3 PUT + Lambda, the frame never touches object storage by
default: cheaper, simpler, and identical on any cloud.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import auth
from .config import AppConfig, Settings
from .providers.base import ObjectStore, ProviderError, VisionProvider, VisionResult
from .repo import Repo

log = logging.getLogger(__name__)

UPLOAD_TTL_S = 30
MAX_JPEG_BYTES = 400_000


@dataclass
class CaptureOutcome:
    ok: bool
    result: VisionResult | None = None
    reason: str = ""          # no_camera | offline | timeout | upload_error | vision_error | low_confidence
    camera_id: str | None = None
    capture_ms: float | None = None
    vision_ms: float | None = None


class CameraService:
    def __init__(self, settings: Settings, cfg: AppConfig, repo: Repo, vision: VisionProvider | None,
                 storage: ObjectStore, publish: Callable[[str, dict], Awaitable[bool]],
                 sessions: dict | None = None):
        self.settings = settings
        self.cfg = cfg
        self.repo = repo
        self.vision = vision
        self.storage = storage
        self.publish = publish
        # Live main-board sessions; a main device that announced ``camera: true`` in its hello
        # (e.g. the PC client) can serve captures itself over its WebSocket when no ESP32-CAM is paired.
        self.sessions = sessions if sessions is not None else {}
        self._pending: dict[str, asyncio.Future[bytes]] = {}

    @property
    def available(self) -> bool:
        return self.vision is not None

    async def pick_camera(self, main_id: str, role: str | None) -> str | None:
        cams = [d for d in await self.repo.list_devices()
                if d.kind == "camera" and d.enabled and d.paired_main == main_id]
        if not cams:
            sess = self.sessions.get(main_id)
            return main_id if sess is not None and getattr(sess, "has_camera", False) else None
        role = role or self.cfg.get("vision.default_camera_role", "user")
        online = [c for c in cams if c.reported.get("online", True)]
        for c in online or cams:
            if c.camera_role == role:
                return c.id
        return (online or cams)[0].id

    def upload_url(self, camera_id: str, job_id: str) -> str:
        token = auth.sign(self.settings.signing_secret, "upload", f"{camera_id}/{job_id}", UPLOAD_TTL_S)
        return f"{self.settings.public_base_url}/v1/camera/{camera_id}/upload/{job_id}?token={token}"

    def verify_upload(self, camera_id: str, job_id: str, token: str) -> bool:
        return job_id in self._pending and auth.verify(
            self.settings.signing_secret, "upload", f"{camera_id}/{job_id}", token)

    def deliver(self, job_id: str, jpeg: bytes) -> bool:
        fut = self._pending.get(job_id)
        if fut is None or fut.done():
            return False
        fut.set_result(jpeg)
        return True

    def fail(self, job_id: str, reason: str) -> None:
        fut = self._pending.get(job_id)
        if fut and not fut.done():
            fut.set_exception(ProviderError(reason))

    async def capture_and_describe(self, main_id: str, question: str, role: str | None = None) -> CaptureOutcome:
        if not self.available:
            return CaptureOutcome(False, reason="no_vision_provider")
        cam = await self.pick_camera(main_id, role)
        if cam is None:
            return CaptureOutcome(False, reason="no_camera")
        job_id = secrets.token_hex(8)
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._pending[job_id] = fut
        t0 = time.monotonic()
        try:
            cmd = {"type": "capture", "job_id": job_id, "upload_url": self.upload_url(cam, job_id),
                   "framesize": "VGA", "quality": 12}
            if cam == main_id and main_id in self.sessions:   # built-in camera on the main device
                await self.sessions[main_id].send_json(cmd)
                sent = True
            else:
                sent = await self.publish(cam, cmd)
            if not sent:
                return CaptureOutcome(False, reason="offline", camera_id=cam)
            timeout = float(self.cfg.get("vision.capture_timeout_s", 6))
            try:
                jpeg = await asyncio.wait_for(fut, timeout)
            except asyncio.TimeoutError:
                return CaptureOutcome(False, reason="timeout", camera_id=cam)
            except ProviderError as e:
                return CaptureOutcome(False, reason=f"upload_error:{e}", camera_id=cam)
            capture_ms = (time.monotonic() - t0) * 1000
            if self.cfg.get("storage.retain_debug_frames", False):
                await self.storage.put(f"frames/{cam}/{job_id}.jpg", jpeg, "image/jpeg")
            t1 = time.monotonic()
            try:
                result = await asyncio.wait_for(self.vision.describe(jpeg, question), 10)
            except (ProviderError, asyncio.TimeoutError) as e:
                log.warning("vision failed", extra={"err": str(e)})
                return CaptureOutcome(False, reason="vision_error", camera_id=cam, capture_ms=capture_ms)
            finally:
                del jpeg  # FR-5.5: raw frame dropped as soon as analysis returns
            vision_ms = (time.monotonic() - t1) * 1000
            if result.confidence < float(self.cfg.get("vision.min_confidence", 0.6)):
                return CaptureOutcome(False, result, "low_confidence", cam, capture_ms, vision_ms)
            return CaptureOutcome(True, result, "", cam, capture_ms, vision_ms)
        finally:
            self._pending.pop(job_id, None)
