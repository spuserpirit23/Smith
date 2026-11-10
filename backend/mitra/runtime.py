"""Process-wide wiring: config, repo, providers, MQTT bus, camera service, live sessions."""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import logging
import time
from typing import TYPE_CHECKING, Any

from .bus.mqtt import MqttBus, write_passwd_file
from .camera import CameraService
from .config import AppConfig, Settings
from .providers.base import ChatMessage
from .providers.registry import Providers
from .repo import Repo, now

if TYPE_CHECKING:
    from .session.conversation import DeviceSession

log = logging.getLogger(__name__)

CONTEXT_IDLE_RESET_S = 15 * 60


class ContextStore:
    """Per-device rolling conversation context (FR-4.2). Survives WebSocket reconnects, lives in
    RAM only, and is cleared after 15 minutes of silence or on an operator delete."""

    def __init__(self, max_turns: int):
        self.max_messages = max_turns * 2
        self._ctx: dict[str, collections.deque[ChatMessage]] = {}
        self._touched: dict[str, float] = {}

    def get(self, device_id: str) -> collections.deque[ChatMessage]:
        if time.monotonic() - self._touched.get(device_id, 0) > CONTEXT_IDLE_RESET_S:
            self._ctx.pop(device_id, None)
        self._touched[device_id] = time.monotonic()
        return self._ctx.setdefault(device_id, collections.deque(maxlen=self.max_messages))

    def seed(self, device_id: str, msgs: list[ChatMessage]) -> None:
        dq = self.get(device_id)
        if not dq:
            dq.extend(msgs[-self.max_messages:])

    def clear(self, device_id: str) -> None:
        self._ctx.pop(device_id, None)


class Runtime:
    def __init__(self, settings: Settings, cfg: AppConfig, repo: Repo, providers: Providers):
        self.settings = settings
        self.cfg = cfg
        self.repo = repo
        self.p = providers
        self.sessions: dict[str, DeviceSession] = {}
        self.context = ContextStore(int(cfg.get("conversation.context_turns", 10)))
        self.bus = MqttBus(settings.mqtt_host, settings.mqtt_port, settings.mqtt_username,
                           settings.mqtt_password, self._on_mqtt)
        self.camera = CameraService(settings, cfg, repo, providers.vision, providers.storage, self.bus.publish,
                                    sessions=self.sessions)
        self._bg: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.repo.init()
        await self.sync_broker_auth()
        await self.bus.start()
        self._bg.append(asyncio.create_task(self._housekeeping(), name="housekeeping"))
        if hasattr(self.p.llm, "warm_up"):
            self._bg.append(asyncio.create_task(self._warm_up_llm(), name="llm-warm-up"))

    async def _warm_up_llm(self) -> None:
        # Retry while a local model server (Ollama) is still starting or pulling the model.
        for attempt in range(1, 31):
            try:
                await self.p.llm.warm_up(self.cfg.system_prompt())  # type: ignore[attr-defined]
                return
            except Exception as e:  # noqa: BLE001
                log.warning("llm warm-up failed, retrying", extra={"attempt": attempt, "err": str(e)})
                await asyncio.sleep(10)

    async def stop(self) -> None:
        for t in self._bg:
            t.cancel()
        for s in list(self.sessions.values()):
            await s.close("server_shutdown")
        await self.bus.stop()
        await self.repo.close()

    async def sync_broker_auth(self) -> None:
        if self.settings.mosquitto_passwd_file:
            devices = await self.repo.list_devices()
            await asyncio.to_thread(write_passwd_file, self.settings.mosquitto_passwd_file,
                                    self.settings.mqtt_username, self.settings.mqtt_password, devices)

    async def _housekeeping(self) -> None:
        while True:
            try:
                n = await self.repo.purge_expired()
                if n:
                    log.info("purged expired history", extra={"rows": n})
            except Exception:  # noqa: BLE001
                log.exception("housekeeping failed")
            await asyncio.sleep(3600)

    # -- MQTT inbound -------------------------------------------------------------------------
    async def _on_mqtt(self, device_id: str, channel: str, payload: dict[str, Any]) -> None:
        dev = await self.repo.get_device(device_id)
        if dev is None:
            return
        if channel == "state":
            reported = {**dev.reported, **payload, "updated_at": now().isoformat()}
            await self.repo.update_device(device_id, reported=reported, last_seen=now(),
                                          fw_version=payload.get("fw", dev.fw_version))
            if dev.kind == "camera" and dev.paired_main in self.sessions:
                active = payload.get("state") == "capturing"
                await self.sessions[dev.paired_main].send_json({"type": "camera", "active": active,
                                                                "camera_id": device_id})
        elif channel == "telemetry":
            await self.repo.update_device(device_id, last_seen=now())
            if payload.get("type") == "capture" and payload.get("status") == "error":
                self.camera.fail(str(payload.get("job_id")), str(payload.get("err", "camera error")))

    # -- operator actions ---------------------------------------------------------------------
    async def clear_history(self, device_id: str) -> int:
        self.context.clear(device_id)
        return await self.repo.delete_history(device_id)

    async def push_config(self, device_id: str) -> None:
        dev = await self.repo.get_device(device_id)
        if dev is None:
            return
        msg = {"type": "config", **dev.config}
        if device_id in self.sessions:
            await self.sessions[device_id].send_json(msg)
        await self.bus.publish(device_id, msg)

    async def kill(self, device_id: str) -> None:
        await self.bus.publish(device_id, {"type": "disable"})
        if device_id in self.sessions:
            await self.sessions[device_id].close("disabled")
        await self.sync_broker_auth()

    def since(self, hours: float) -> dt.datetime:
        return now() - dt.timedelta(hours=hours)
