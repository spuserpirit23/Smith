"""End-to-end conversation tests over a real WebSocket against a real uvicorn server, using mock
providers. Covers PRD scenarios 1-4 and 6, plus fault injection (FR-4.4) and history controls."""
import asyncio

import pytest

from mitra.providers.stt import MockSTT
from mitra.sim import SimDevice, silence, speech_like, wake_tone

UTTER = speech_like(1000)


async def connect(stack, device_id="mitra-001", wake="ptt", **kw) -> SimDevice:
    secret = await stack.provision(device_id, **kw)
    dev = SimDevice(stack.ws_url, device_id, secret, wake=wake, speed=4)
    await dev.connect()
    return dev


async def test_rejects_bad_credentials(stack):
    await stack.provision("mitra-001")
    dev = SimDevice(stack.ws_url, "mitra-001", "wrong-secret")
    with pytest.raises(Exception):
        await dev.connect()
        await dev.ws.recv()


async def test_ptt_full_turn_and_follow_up_window(stack):
    dev = await connect(stack)
    assert dev.hello["type"] == "hello" and dev.hello["audio"]["sample_rate"] == 16000
    await dev.ptt_turn(UTTER)
    await dev.wait_for("state", lambda m: m["state"] == "processing")
    stt = await dev.wait_for("stt")
    assert stt["text"].startswith("hello mitra")
    await dev.wait_for("tts", lambda m: m.get("state") == "start")
    sentence = await dev.wait_for("tts", lambda m: m.get("state") == "sentence")
    assert "hello mitra" in sentence["text"].lower()
    await dev.wait_for("tts", lambda m: m.get("state") == "stop")
    assert len(dev.replies[-1]) > 3200
    # Follow-up window opens, then closes silently with no speech -> idle without a cue.
    await dev.wait_for("state", lambda m: m["state"] == "listening" and m.get("mode") == "follow_up")
    await dev.stream(silence(600))
    await dev.wait_for("state", lambda m: m["state"] == "idle", timeout=4)
    assert not [m for m in dev.log if m["type"] == "cue"]
    await dev.close()

    lat = (await stack.http.get("/v1/admin/latency")).json()
    assert lat["turns"] >= 1 and lat["outcomes"].get("ok") == 1
    assert lat["end_to_first_audio_ms"]["n"] == 1


async def test_context_is_kept_between_turns(stack):
    dev = await connect(stack)
    MockSTT.script[:] = ["my name is Sky", "what is my name"]
    for _ in range(2):
        await dev.ptt_turn(UTTER)
        await dev.wait_for("tts", lambda m: m.get("state") == "stop")
        await dev.wait_for("state", lambda m: m["state"] == "listening")
        await dev.send_json({"type": "abort"})
        await dev.wait_for("state", lambda m: m["state"] == "idle")
    msgs = stack.rt.p.llm.last_messages
    assert [m.role for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0].content == "my name is Sky"
    assert "companion" in stack.rt.p.llm.last_system and "112" in stack.rt.p.llm.last_system
    await dev.close()


async def test_cloud_wake_then_no_speech_returns_to_idle_with_cue(stack):
    dev = await connect(stack, wake="cloud")
    assert dev.hello["wake"] == "cloud"
    await dev.stream(silence(200) + wake_tone(400))
    wake = await dev.wait_for("wake")
    assert wake["engine"] == "cloud" and wake["activation_id"] >= 1
    await dev.wait_for("state", lambda m: m["state"] == "listening")
    await dev.stream(silence(2000))            # FR-1.3: nothing said after wake
    cue = await dev.wait_for("cue", timeout=4)
    assert cue["name"] == "standby"
    await dev.wait_for("state", lambda m: m["state"] == "idle")
    acts = (await stack.http.get("/v1/admin/activations")).json()
    assert len(acts) == 1
    r = await stack.http.post(f"/v1/admin/activations/{acts[0]['id']}/label", json={"correct": True})
    assert r.json()["label"] is True
    await dev.close()


async def test_cloud_wake_full_turn_with_server_vad(stack):
    dev = await connect(stack, wake="cloud")
    await dev.stream(silence(200) + wake_tone(400))
    await dev.wait_for("wake")
    await dev.stream(silence(200) + UTTER + silence(900))
    await dev.wait_for("stt")
    await dev.wait_for("tts", lambda m: m.get("state") == "stop")
    await dev.close()


async def test_idle_audio_without_wake_never_reaches_stt(stack):
    dev = await connect(stack, wake="cloud")
    MockSTT.script[:] = ["SHOULD NOT BE USED"]
    await dev.stream(UTTER + silence(500))
    await asyncio.sleep(0.3)
    assert not [m for m in dev.log if m["type"] in ("wake", "stt")]
    assert MockSTT.script == ["SHOULD NOT BE USED"]   # FR-2.2: nothing sent to STT pre-wake
    await dev.close()


async def test_unclear_speech_asks_to_repeat_twice_then_gives_up(stack):
    dev = await connect(stack)
    MockSTT.script[:] = ["", "", ""]                  # FR-1.4
    await dev.ptt_turn(UTTER)
    assert (await dev.wait_for("cue"))["name"] == "repeat"
    await dev.wait_for("state", lambda m: m["state"] == "listening" and m.get("mode") == "repeat")
    await dev.stream(UTTER + silence(800))
    assert (await dev.wait_for("cue"))["name"] == "repeat"
    await dev.wait_for("state", lambda m: m["state"] == "listening")
    await dev.stream(UTTER + silence(800))
    assert (await dev.wait_for("cue"))["name"] == "standby"
    await dev.wait_for("state", lambda m: m["state"] == "idle")
    await dev.close()


@pytest.mark.parametrize("phrase,code", [("trigger llm timeout", "llm_timeout"),
                                         ("trigger llm error", "llm_error"),
                                         ("__slow__", "stt_timeout")])
async def test_fault_injection_plays_fallback(stack, phrase, code):
    dev = await connect(stack)
    MockSTT.script[:] = [phrase]
    await dev.ptt_turn(UTTER)
    err = await dev.wait_for("error", timeout=6)
    assert err["code"] == code and err["cue"] == "no_brain"
    await dev.wait_for("state", lambda m: m["state"] == "idle")
    # the device stays usable afterwards
    await dev.ptt_turn(UTTER)
    await dev.wait_for("tts", lambda m: m.get("state") == "stop")
    await dev.close()


async def test_tts_failure_on_all_providers_plays_fallback(stack):
    dev = await connect(stack)
    MockSTT.script[:] = ["please make tts fail now"]
    await dev.ptt_turn(UTTER)
    err = await dev.wait_for("error", timeout=6)
    assert err["code"] == "tts_error"
    await dev.close()


async def test_twenty_consecutive_cycles(stack):
    """Milestone B exit criterion, in software: 20 cycles on one connection."""
    dev = await connect(stack)
    dev.speed = 10
    for _ in range(20):
        await dev.ptt_turn(speech_like(400))
        await dev.wait_for("tts", lambda m: m.get("state") == "stop")
        await dev.wait_for("state", lambda m: m["state"] == "listening")
        await dev.send_json({"type": "abort"})
        await dev.wait_for("state", lambda m: m["state"] == "idle")
    lat = (await stack.http.get("/v1/admin/latency")).json()
    assert lat["outcomes"]["ok"] == 20
    await dev.close()


async def test_camera_question_injects_observation(stack):
    secret = await stack.provision("mitra-001")
    await stack.provision("cam-001", kind="camera", paired_main="mitra-001", camera_role="user")

    async def fake_publish(device_id, payload):
        assert device_id == "cam-001" and payload["type"] == "capture"
        async def upload():
            await asyncio.sleep(0.05)
            r = await stack.http.post(payload["upload_url"].removeprefix(stack.base), content=b"\xff\xd8fakejpeg\xff\xd9",
                                      headers={"Content-Type": "image/jpeg"})
            assert r.status_code == 200, r.text
        asyncio.create_task(upload())
        return True

    stack.rt.camera.publish = fake_publish
    dev = SimDevice(stack.ws_url, "mitra-001", secret, speed=4)
    await dev.connect()
    MockSTT.script[:] = ["what do you see"]
    await dev.ptt_turn(UTTER)
    await dev.wait_for("tts", lambda m: m.get("state") == "stop")
    last = stack.rt.p.llm.last_messages[-1].content
    assert "[Camera observation] A desk with a laptop" in last
    await dev.close()


async def test_builtin_camera_on_main_device_over_websocket(stack):
    """PC client / dev boards: no ESP32-CAM paired, main device announces camera:true and serves
    the capture over its own WebSocket."""
    secret = await stack.provision("mitra-001")
    dev = SimDevice(stack.ws_url, "mitra-001", secret, speed=4, camera=True)
    await dev.connect()

    async def serve_capture():
        cmd = await dev.wait_for("capture", timeout=5)
        r = await stack.http.post(cmd["upload_url"].removeprefix(stack.base), content=b"\xff\xd8fakejpeg\xff\xd9",
                                  headers={"Content-Type": "image/jpeg"})
        assert r.status_code == 200, r.text

    cam = asyncio.create_task(serve_capture())
    r = (await stack.http.post("/v1/admin/devices/mitra-001/look")).json()
    await cam
    assert r["ok"] is True and r["camera"] == "mitra-001" and "desk" in r["description"]
    await dev.close()


async def test_camera_upload_rejects_bad_token(stack):
    r = await stack.http.post("/v1/camera/cam-001/upload/abc?token=1.x", content=b"\xff\xd8")
    assert r.status_code == 403


async def test_camera_missing_is_spoken_not_crashed(stack):
    dev = await connect(stack)
    MockSTT.script[:] = ["what can you see"]
    await dev.ptt_turn(UTTER)
    await dev.wait_for("tts", lambda m: m.get("state") == "stop")
    assert "camera is not available" in stack.rt.p.llm.last_messages[-1].content
    await dev.close()


async def test_history_delete_and_kill_switch(stack):
    dev = await connect(stack, history_opt_in=True)
    await dev.ptt_turn(UTTER)
    await dev.wait_for("tts", lambda m: m.get("state") == "stop")
    await asyncio.sleep(0.1)
    assert await stack.rt.repo.recent_turns("mitra-001", 10)
    r = await stack.http.delete("/v1/admin/devices/mitra-001/history")
    assert r.json()["deleted_turns"] == 2
    assert await stack.rt.repo.recent_turns("mitra-001", 10) == []
    r = await stack.http.patch("/v1/admin/devices/mitra-001", json={"enabled": False})
    assert r.json()["enabled"] is False
    await dev.wait_for("goodbye", timeout=3)
    r = await stack.http.delete("/v1/admin/devices/mitra-001/data")
    assert r.status_code == 200 and r.json()["metrics"] >= 1


async def test_history_not_persisted_without_opt_in(stack):
    dev = await connect(stack)
    await dev.ptt_turn(UTTER)
    await dev.wait_for("tts", lambda m: m.get("state") == "stop")
    assert await stack.rt.repo.recent_turns("mitra-001", 10) == []
    await dev.close()


async def test_admin_requires_token(stack):
    import httpx
    async with httpx.AsyncClient(base_url=stack.base) as c:
        assert (await c.get("/v1/admin/devices")).status_code == 401
        assert (await c.get("/healthz")).json()["ok"] is True


async def test_status_report_updates_shadow_and_ota_goes_over_ws(stack):
    dev = await connect(stack)
    await dev.send_json({"type": "status", "state": "idle", "fw": "0.1.0", "rssi": -55, "min_heap": 41000})
    await asyncio.sleep(0.2)
    d = {x["id"]: x for x in (await stack.http.get("/v1/admin/devices")).json()}["mitra-001"]
    assert d["reported"]["rssi"] == -55 and d["fw_version"] == "0.1.0" and d["online"] is True
    r = await stack.http.post("/v1/admin/devices/mitra-001/ota", data={"version": "0.2.0"},
                              files={"firmware": ("fw.bin", b"\xe9firmware-bytes", "application/octet-stream")})
    assert r.status_code == 200 and r.json()["published"] is True
    ota = await dev.wait_for("ota")
    import httpx
    async with httpx.AsyncClient() as c:
        img = await c.get(ota["url"])
    assert img.status_code == 200 and img.content == b"\xe9firmware-bytes"
    import hashlib
    assert hashlib.sha256(img.content).hexdigest() == ota["sha256"]
    async with httpx.AsyncClient() as c:   # token is bound to device + version
        bad = await c.get(ota["url"].replace("/0.2.0?", "/0.3.0?"))
    assert bad.status_code == 403
    r = await stack.http.patch("/v1/admin/devices/mitra-001", json={"config": {"volume": 55}})
    cfg = await dev.wait_for("config")
    assert cfg["volume"] == 55
    await dev.close()


async def test_hands_free_greets_then_keeps_listening(stack):
    secret = await stack.provision("pc-001")
    dev = SimDevice(stack.ws_url, "pc-001", secret, speed=4,
                    extra_hello={"greet": True, "hands_free": True})
    await dev.connect()
    # 1. Greeting is spoken right after connect, then listening opens without any button.
    greet = await dev.wait_for("tts", lambda m: m.get("state") == "sentence")
    assert "how are you" in greet["text"].lower()
    await dev.wait_for("tts", lambda m: m.get("state") == "stop")
    await dev.wait_for("state", lambda m: m["state"] == "listening" and m.get("mode") == "converse")
    # 2. Speak; VAD ends the turn, Mitra replies, and listening reopens hands-free.
    MockSTT.script[:] = ["I am good, thanks"]
    await dev.stream(UTTER + silence(900))
    stt = await dev.wait_for("stt")
    assert stt["text"] == "I am good, thanks"
    await dev.wait_for("tts", lambda m: m.get("state") == "stop")
    await dev.wait_for("state", lambda m: m["state"] == "listening" and m.get("mode") == "converse")
    # 3. Silence -> goes idle quietly (no cue).
    await dev.stream(silence(400))
    await dev.wait_for("state", lambda m: m["state"] == "idle", timeout=5)
    assert not [m for m in dev.log if m["type"] in ("cue", "error")]
    # The greeting is part of the LLM context, so the model knows it asked.
    ctx = stack.rt.context.get("pc-001")
    assert ctx[0].role == "assistant" and "How are you" in ctx[0].content
    await dev.close()


async def test_builtin_camera_failure_is_reported_fast(stack):
    """A webcam that fails to capture reports it over the WebSocket; the look fails right away
    instead of waiting out capture_timeout_s."""
    import time

    secret = await stack.provision("mitra-001")
    dev = SimDevice(stack.ws_url, "mitra-001", secret, speed=4, camera=True)
    await dev.connect()

    async def fail_capture():
        cmd = await dev.wait_for("capture", timeout=5)
        await dev.send_json({"type": "capture", "job_id": cmd["job_id"], "status": "error",
                             "err": "cannot open webcam #0"})

    cam = asyncio.create_task(fail_capture())
    t0 = time.monotonic()
    r = (await stack.http.post("/v1/admin/devices/mitra-001/look")).json()
    await cam
    assert r["ok"] is False and r["reason"].startswith("upload_error") and "webcam" in r["reason"]
    assert time.monotonic() - t0 < 1.5
    await dev.close()
