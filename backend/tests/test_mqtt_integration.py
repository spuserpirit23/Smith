"""Real-broker integration: password file + ACLs + camera capture over MQTT.

Runs only when MITRA_TEST_MOSQUITTO is set to the path of a ``mosquitto`` binary. The test starts
a private broker with deploy/mosquitto/{mosquitto.conf,acl} (paths rewritten to a temp dir).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import aiomqtt
import httpx
import pytest

from mitra.config import Settings, load_app_config
from mitra.main import create_app
from mitra.providers.registry import build_providers
from mitra.providers.stt import MockSTT
from mitra.repo import MemoryRepo
from mitra.runtime import Runtime
from mitra.sim import SimDevice, speech_like

from conftest import ADMIN, free_port, write_test_config

MOSQ = os.environ.get("MITRA_TEST_MOSQUITTO")
pytestmark = pytest.mark.skipif(not MOSQ, reason="set MITRA_TEST_MOSQUITTO=/usr/sbin/mosquitto")
DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "mosquitto"


@pytest.fixture
async def broker(tmp_path):
    port, ws_port = free_port(), free_port()
    (tmp_path / "auth").mkdir()
    (tmp_path / "data").mkdir()
    conf = (DEPLOY / "mosquitto.conf").read_text()
    # Test runs as root: keep root (production entrypoint chowns to mosquitto instead) and read the
    # backend-written file directly (production copies it to /mosquitto/run/ first).
    conf = (conf.replace("/mosquitto/run/passwd", str(tmp_path / "auth/passwd"))
                .replace("/mosquitto/run/acl", str(tmp_path / "acl"))
                .replace("/mosquitto/data/", str(tmp_path / "data") + "/")
                .replace("listener 1883 0.0.0.0", f"listener {port} 127.0.0.1")
                .replace("listener 9001 0.0.0.0", f"listener {ws_port} 127.0.0.1")) + "\nuser root\n"
    (tmp_path / "mosquitto.conf").write_text(conf)
    shutil.copy(DEPLOY / "acl", tmp_path / "acl")
    yield {"port": port, "dir": tmp_path, "conf": tmp_path / "mosquitto.conf"}


async def wait_port(port: int) -> None:
    for _ in range(100):
        try:
            _, w = await asyncio.open_connection("127.0.0.1", port)
            w.close()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise RuntimeError("broker did not start")


async def test_camera_capture_over_real_mqtt(broker, tmp_path):
    MockSTT.script.clear()
    cfg = load_app_config(write_test_config(tmp_path / "cfg"))
    api_port = free_port()
    settings = Settings(admin_token=ADMIN, signing_secret="s", public_base_url=f"http://127.0.0.1:{api_port}",
                        mqtt_host="127.0.0.1", mqtt_port=broker["port"], mqtt_password="backend-pw",
                        mosquitto_passwd_file=str(broker["dir"] / "auth/passwd"), log_json=False,
                        log_level="WARNING")
    rt = Runtime(settings, cfg, MemoryRepo(), build_providers(cfg))
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(create_app(settings, rt), host="127.0.0.1", port=api_port,
                                           log_level="warning"))
    srv_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    # Backend wrote the passwd file during startup; now start the broker on it.
    proc = subprocess.Popen([MOSQ, "-c", str(broker["conf"])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await wait_port(broker["port"])
        http = httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}", headers={"Authorization": f"Bearer {ADMIN}"})
        main_secret = (await http.post("/v1/admin/devices", json={"id": "mitra-001"})).json()["secret"]
        cam_secret = (await http.post("/v1/admin/devices", json={
            "id": "cam-001", "kind": "camera", "paired_main": "mitra-001", "camera_role": "user"})).json()["secret"]
        proc.send_signal(1)  # what deploy/mosquitto/entrypoint.sh does when the file changes
        await asyncio.sleep(0.5)

        # Wrong password is refused.
        with pytest.raises(aiomqtt.MqttError):
            async with aiomqtt.Client("127.0.0.1", broker["port"], username="cam-001", password="nope"):
                pass

        captured = asyncio.Event()

        async def fake_camera():
            async with aiomqtt.Client("127.0.0.1", broker["port"], username="cam-001", password=cam_secret,
                                      identifier="cam-001") as c:
                await c.subscribe("mitra/cam-001/cmd", qos=1)
                # ACL: a camera must not be able to read another device's commands.
                await c.subscribe("mitra/mitra-001/cmd", qos=1)
                await c.publish("mitra/cam-001/state", json.dumps({"state": "idle", "online": True, "fw": "cam-0.1"}),
                                qos=1, retain=True)
                async for msg in c.messages:
                    assert str(msg.topic) == "mitra/cam-001/cmd"
                    cmd = json.loads(msg.payload)
                    if cmd["type"] != "capture":
                        continue
                    async with httpx.AsyncClient() as h:
                        r = await h.post(cmd["upload_url"], content=b"\xff\xd8camera-frame\xff\xd9",
                                         headers={"Content-Type": "image/jpeg"})
                        assert r.status_code == 200
                    captured.set()

        cam_task = asyncio.create_task(fake_camera())
        await asyncio.sleep(0.5)
        # Operator smoke test endpoint
        r = (await http.post("/v1/admin/devices/mitra-001/look")).json()
        assert r["ok"] is True and r["camera"] == "cam-001" and "desk" in r["description"]
        assert captured.is_set()
        # Reported state reached the device shadow.
        devs = {d["id"]: d for d in (await http.get("/v1/admin/devices")).json()}
        assert devs["cam-001"]["fw_version"] == "cam-0.1"

        # Full spoken flow through the main board.
        dev = SimDevice(f"ws://127.0.0.1:{api_port}/v1/device/ws", "mitra-001", main_secret, speed=4)
        await dev.connect()
        MockSTT.script[:] = ["what do you see"]
        await dev.ptt_turn(speech_like(800))
        await dev.wait_for("tts", lambda m: m.get("state") == "stop", timeout=8)
        assert "[Camera observation] A desk" in rt.p.llm.last_messages[-1].content
        await dev.close()

        # Kill switch removes the camera from the broker's password file.
        await http.patch("/v1/admin/devices/cam-001", json={"enabled": False})
        assert "cam-001:" not in (broker["dir"] / "auth/passwd").read_text()
        cam_task.cancel()
        await http.aclose()
    finally:
        proc.terminate()
        proc.wait(5)
        server.should_exit = True
        await srv_task
