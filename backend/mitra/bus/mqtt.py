"""MQTT control plane on a self-hosted Mosquitto (replaces AWS IoT Core).

Topics (device id == MQTT username; ACLs pin each device to its own subtree):
  mitra/<id>/cmd        backend -> device   commands: capture, config, ota, disable, reboot
  mitra/<id>/state      device  -> backend  retained "reported" shadow (state, fw, rssi, mute...)
  mitra/<id>/telemetry  device  -> backend  periodic stats; cameras also post capture status here

Mosquitto is a plain MQTT 3.1.1/5 broker, so the same firmware could later point at IoT Core,
EMQX, HiveMQ, or any managed broker without code changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from typing import Any, Awaitable, Callable

from ..auth import mosquitto_hash
from ..repo import Device

log = logging.getLogger(__name__)

Handler = Callable[[str, str, dict[str, Any]], Awaitable[None]]


def write_passwd_file(path: str, backend_user: str, backend_password: str, devices: list[Device]) -> None:
    """Atomically rewrite Mosquitto's password file from the device table. The broker container
    reloads it on change (see deploy/mosquitto/entrypoint.sh)."""
    lines = [f"{backend_user}:{mosquitto_hash(backend_password)}"]
    lines += [f"{d.id}:{d.mqtt_hash}" for d in devices if d.enabled and d.mqtt_hash]
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".passwd.")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    # Owner-only. The broker's entrypoint (root) copies it into a mosquitto-owned 0600 file.
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class MqttBus:
    def __init__(self, host: str, port: int, username: str, password: str, on_message: Handler):
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.on_message = on_message
        self._client = None
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    async def start(self) -> None:
        if self.enabled:
            self._task = asyncio.create_task(self._run(), name="mqtt-bus")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        import aiomqtt

        backoff = 1.0
        while True:
            try:
                async with aiomqtt.Client(
                    self.host, self.port, username=self.username, password=self.password,
                    identifier="mitra-backend", keepalive=30,
                ) as client:
                    self._client = client
                    self._connected.set()
                    backoff = 1.0
                    await client.subscribe("mitra/+/state", qos=1)
                    await client.subscribe("mitra/+/telemetry", qos=0)
                    log.info("mqtt connected", extra={"host": self.host})
                    async for msg in client.messages:
                        parts = str(msg.topic).split("/")
                        if len(parts) != 3:
                            continue
                        try:
                            payload = json.loads(msg.payload or b"{}")
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        try:
                            await self.on_message(parts[1], parts[2], payload)
                        except Exception:  # noqa: BLE001
                            log.exception("mqtt handler failed")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._connected.clear()
                self._client = None
                log.warning("mqtt disconnected, retrying", extra={"err": str(e), "backoff_s": backoff})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def publish(self, device_id: str, payload: dict[str, Any], *, retain: bool = False) -> bool:
        if not self.enabled:
            return False
        try:
            await asyncio.wait_for(self._connected.wait(), 3)
        except asyncio.TimeoutError:
            return False
        assert self._client is not None
        await self._client.publish(f"mitra/{device_id}/cmd", json.dumps(payload), qos=1, retain=retain)
        return True
