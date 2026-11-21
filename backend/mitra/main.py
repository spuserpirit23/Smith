"""HTTP + WebSocket entry point.

  WS   /v1/device/ws                          main-board audio + control (protocol: docs/PROTOCOL.md)
  POST /v1/camera/{cam}/upload/{job}?token=   one-shot JPEG upload from an ESP32-CAM
  GET  /v1/ota/{device}/{version}?token=      firmware image for OTA
  *    /v1/admin/...                          operator API (Bearer MITRA_ADMIN_TOKEN)
  GET  /healthz, /metrics
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.responses import PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect, WebSocketState

from . import auth
from .api.provisioning import make_device, nvs_csv
from .camera import MAX_JPEG_BYTES
from .config import Settings, get_settings, load_app_config
from .observability import CONNECTED, setup_logging
from .providers.registry import build_providers
from .repo import METRIC_FIELDS, make_repo, summarize_latency
from .runtime import Runtime
from .session.conversation import DeviceSession

log = logging.getLogger("mitra")


class ProvisionIn(BaseModel):
    id: str
    kind: str = "main"
    name: str = ""
    paired_main: str | None = None
    camera_role: str | None = None
    history_opt_in: bool = False

class DevicePatch(BaseModel):
    enabled: bool | None = None
    history_opt_in: bool | None = None
    name: str | None = None
    camera_role: str | None = None
    config: dict[str, Any] | None = None

class LabelIn(BaseModel):
    correct: bool


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level, settings.log_json)
        rt = runtime
        if rt is None:
            if not settings.admin_token or not settings.signing_secret:
                raise RuntimeError("MITRA_ADMIN_TOKEN and MITRA_SIGNING_SECRET must be set")
            cfg = load_app_config(settings.config_file)
            rt = Runtime(settings, cfg, make_repo(settings.database_url), build_providers(cfg))
        app.state.rt = rt
        await rt.start()
        log.info("mitra backend started", extra={
            "stt": rt.p.stt.name, "llm": rt.p.llm.name, "tts": [p.name for p in rt.p.tts.providers],
            "wake": rt.p.wake.name if rt.p.wake else None, "vision": rt.p.vision.name if rt.p.vision else None,
            "mqtt": bool(settings.mqtt_host), "db": ("file" if settings.database_url.startswith("file:") else "postgres") if settings.database_url else "memory"})
        try:
            yield
        finally:
            await rt.stop()

    app = FastAPI(title="Mitra backend", version="0.1.0", lifespan=lifespan)

    def rt_of(request: Request) -> Runtime:
        return request.app.state.rt

    def admin(authorization: str | None = Header(default=None)) -> None:
        if not auth.check_admin(authorization, settings.admin_token):
            raise HTTPException(401, "admin token required")

    # ------------------------------------------------------------------ health / metrics
    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        rt = rt_of(request)
        return {"ok": True, "connected": len(rt.sessions)}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------------ device audio socket
    @app.websocket("/v1/device/ws")
    async def device_ws(ws: WebSocket) -> None:
        rt: Runtime = ws.app.state.rt
        device_id = ws.headers.get("x-device-id", "")
        authz = ws.headers.get("authorization", "")
        dev = await rt.repo.get_device(device_id) if auth.valid_device_id(device_id) else None
        if (dev is None or dev.kind != "main" or not authz.startswith("Bearer ")
                or not auth.check_secret(authz[7:], dev.secret_hash)):
            await ws.close(code=4401, reason="unauthorized")
            return
        if not dev.enabled:
            await ws.close(code=4403, reason="disabled")
            return
        await ws.accept()
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), 5))
            if hello.get("type") != "hello":
                raise ValueError("first message must be hello")
            session = DeviceSession(rt, dev, hello, ws.send_text, ws.send_bytes,
                                    lambda code, reason: ws.close(code=code, reason=reason))
        except (asyncio.TimeoutError, ValueError, json.JSONDecodeError, WebSocketDisconnect) as e:
            log.warning("bad hello", extra={"device": device_id, "err": str(e)})
            with contextlib.suppress(Exception):
                await ws.close(code=4400, reason=str(e)[:100])
            return

        old = rt.sessions.get(dev.id)
        if old:
            await old.close("replaced")
        rt.sessions[dev.id] = session
        CONNECTED.set(len(rt.sessions))
        fw = hello.get("fw")
        with contextlib.suppress(Exception):
            from .repo import now
            await rt.repo.update_device(dev.id, last_seen=now(), **({"fw_version": fw} if fw else {}))
        log.info("device connected", extra={"device": dev.id, "fw": fw, "wake": hello.get("wake")})
        await session.start()
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await session.on_audio(msg["bytes"])
                elif msg.get("text") is not None:
                    try:
                        payload = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    await session.on_message(payload)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("device socket error", extra={"device": dev.id})
        finally:
            await session.close("disconnected")
            if rt.sessions.get(dev.id) is session:
                rt.sessions.pop(dev.id, None)
            CONNECTED.set(len(rt.sessions))
            if ws.client_state != WebSocketState.DISCONNECTED:
                with contextlib.suppress(Exception):
                    await ws.close()
            log.info("device disconnected", extra={"device": dev.id})

    # ------------------------------------------------------------------ camera upload
    @app.post("/v1/camera/{camera_id}/upload/{job_id}")
    async def camera_upload(camera_id: str, job_id: str, request: Request, token: str = Query(...)) -> dict:
        rt = rt_of(request)
        if not rt.camera.verify_upload(camera_id, job_id, token):
            raise HTTPException(403, "invalid or expired upload token")
        body = bytearray()
        async for part in request.stream():
            body += part
            if len(body) > MAX_JPEG_BYTES:
                raise HTTPException(413, "frame too large")
        if not body.startswith(b"\xff\xd8"):
            raise HTTPException(415, "expected image/jpeg")
        rt.camera.deliver(job_id, bytes(body))
        return {"ok": True}

    # ------------------------------------------------------------------ OTA download
    @app.get("/v1/ota/{device_id}/{version}")
    async def ota_download(device_id: str, version: str, request: Request, token: str = Query(...)) -> Response:
        rt = rt_of(request)
        if not auth.verify(settings.signing_secret, "ota", f"{device_id}/{version}", token):
            raise HTTPException(403, "invalid or expired token")
        dev = await rt.repo.get_device(device_id)
        if dev is None:
            raise HTTPException(404)
        try:
            data = await rt.p.storage.get(f"firmware/{dev.kind}/{version}.bin")
        except FileNotFoundError:
            raise HTTPException(404, "no such firmware") from None
        return Response(data, media_type="application/octet-stream")

    # ------------------------------------------------------------------ admin API

    @app.post("/v1/admin/devices", dependencies=[Depends(admin)])
    async def provision(body: ProvisionIn, request: Request) -> dict:
        rt = rt_of(request)
        if body.paired_main and not await rt.repo.get_device(body.paired_main):
            raise HTTPException(400, "paired_main does not exist")
        try:
            dev, secret = make_device(body.id, body.kind, body.name, body.paired_main, body.camera_role,
                                      body.history_opt_in)
            await rt.repo.create_device(dev)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        await rt.sync_broker_auth()
        return {"id": dev.id, "kind": dev.kind, "secret": secret,
                "nvs_csv": nvs_csv(dev, secret, settings.public_base_url, settings.mqtt_public_url),
                "note": "The secret is shown once. Flash it with tools/flash_credentials.sh; do not commit it."}

    def device_view(d) -> dict:
        return {"id": d.id, "kind": d.kind, "name": d.name, "enabled": d.enabled,
                "history_opt_in": d.history_opt_in, "config": d.config, "reported": d.reported,
                "paired_main": d.paired_main, "camera_role": d.camera_role, "fw_version": d.fw_version,
                "last_seen": d.last_seen, "online": None}

    @app.get("/v1/admin/devices", dependencies=[Depends(admin)])
    async def list_devices(request: Request) -> list[dict]:
        rt = rt_of(request)
        out = []
        for d in await rt.repo.list_devices():
            v = device_view(d)
            v["online"] = d.id in rt.sessions if d.kind == "main" else d.reported.get("online")
            out.append(v)
        return out


    @app.patch("/v1/admin/devices/{device_id}", dependencies=[Depends(admin)])
    async def patch_device(device_id: str, body: DevicePatch, request: Request) -> dict:
        rt = rt_of(request)
        dev = await rt.repo.get_device(device_id)
        if dev is None:
            raise HTTPException(404)
        fields = body.model_dump(exclude_none=True)
        if "config" in fields:
            fields["config"] = {**dev.config, **fields["config"]}
        await rt.repo.update_device(device_id, **fields)
        if fields.get("enabled") is False:
            await rt.kill(device_id)                    # kill switch
        elif fields.get("enabled") is True:
            await rt.sync_broker_auth()
        if "config" in fields:
            await rt.push_config(device_id)
        return device_view(await rt.repo.get_device(device_id))

    @app.delete("/v1/admin/devices/{device_id}/history", dependencies=[Depends(admin)])
    async def delete_history(device_id: str, request: Request) -> dict:   # FR-4.3
        rt = rt_of(request)
        if await rt.repo.get_device(device_id) is None:
            raise HTTPException(404)
        return {"deleted_turns": await rt.clear_history(device_id)}

    @app.delete("/v1/admin/devices/{device_id}/data", dependencies=[Depends(admin)])
    async def delete_all_data(device_id: str, request: Request) -> dict:
        rt = rt_of(request)
        dev = await rt.repo.get_device(device_id)
        if dev is None:
            raise HTTPException(404)
        rt.context.clear(device_id)
        res = await rt.repo.delete_device_data(device_id)
        res["frames"] = await rt.p.storage.delete_prefix(f"frames/{device_id}/")
        return res

    @app.delete("/v1/admin/devices/{device_id}", dependencies=[Depends(admin)])
    async def deprovision(device_id: str, request: Request) -> dict:
        rt = rt_of(request)
        if await rt.repo.get_device(device_id) is None:
            raise HTTPException(404)
        await rt.kill(device_id)
        rt.context.clear(device_id)
        await rt.p.storage.delete_prefix(f"frames/{device_id}/")
        await rt.repo.delete_device(device_id)
        await rt.sync_broker_auth()
        return {"deleted": device_id}

    @app.post("/v1/admin/devices/{device_id}/ota", dependencies=[Depends(admin)])
    async def start_ota(device_id: str, request: Request, version: str = Form(...),
                        firmware: UploadFile = File(...)) -> dict:
        rt = rt_of(request)
        dev = await rt.repo.get_device(device_id)
        if dev is None:
            raise HTTPException(404)
        if not re.fullmatch(r"\d+\.\d+\.\d+(-[A-Za-z0-9.]+)?", version):
            raise HTTPException(400, "version must look like 0.2.0")
        data = await firmware.read()
        sha = hashlib.sha256(data).hexdigest()
        await rt.p.storage.put(f"firmware/{dev.kind}/{version}.bin", data)
        token = auth.sign(settings.signing_secret, "ota", f"{device_id}/{version}", 3600)
        url = f"{settings.public_base_url}/v1/ota/{device_id}/{version}?token={token}"
        cmd = {"type": "ota", "url": url, "version": version, "sha256": sha}
        sent = await rt.bus.publish(device_id, cmd)
        if device_id in rt.sessions:            # main boards take control messages over the WebSocket
            await rt.sessions[device_id].send_json(cmd)
            sent = True
        return {"published": sent, "version": version, "sha256": sha, "bytes": len(data)}

    @app.post("/v1/admin/devices/{device_id}/command", dependencies=[Depends(admin)])
    async def command(device_id: str, body: dict[str, Any], request: Request) -> dict:
        """Raw MQTT command passthrough for operators: reboot, capture tests, etc."""
        rt = rt_of(request)
        if await rt.repo.get_device(device_id) is None:
            raise HTTPException(404)
        if body.get("type") not in ("reboot", "config", "identify", "capture"):
            raise HTTPException(400, "unsupported command")
        sent = await rt.bus.publish(device_id, body)
        if device_id in rt.sessions:
            await rt.sessions[device_id].send_json(body)
            sent = True
        return {"published": sent}

    @app.post("/v1/admin/devices/{device_id}/look", dependencies=[Depends(admin)])
    async def test_look(device_id: str, request: Request, role: str | None = None) -> dict:
        """Camera smoke test without speaking: capture via a paired camera and describe."""
        rt = rt_of(request)
        out = await rt.camera.capture_and_describe(device_id, "What do you see?", role)
        return {"ok": out.ok, "reason": out.reason, "camera": out.camera_id,
                "description": out.result.description if out.result else None,
                "confidence": out.result.confidence if out.result else None,
                "capture_ms": out.capture_ms, "vision_ms": out.vision_ms}

    @app.get("/v1/admin/latency", dependencies=[Depends(admin)])
    async def latency(request: Request, device_id: str | None = None, hours: float = 24) -> dict:
        rt = rt_of(request)
        rows = await rt.repo.list_metrics(device_id, rt.since(hours))
        return {"window_hours": hours, "device_id": device_id, "stages": list(METRIC_FIELDS),
                **summarize_latency(rows)}

    @app.get("/v1/admin/turns.csv", dependencies=[Depends(admin)])
    async def turns_csv(request: Request, device_id: str | None = None, hours: float = 24) -> PlainTextResponse:
        rt = rt_of(request)
        rows = await rt.repo.list_metrics(device_id, rt.since(hours))
        cols = ["created_at", "device_id", "session_id", "turn", "mode", "outcome", "end_reason", "stt", "llm",
                "tts", "stt_confidence", "audio_in_ms", "audio_out_ms", "reply_chars", "vision", *METRIC_FIELDS]
        lines = [",".join(cols)] + [",".join("" if r.get(c) is None else str(r.get(c)) for c in cols) for r in rows]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/csv")

    @app.get("/v1/admin/activations", dependencies=[Depends(admin)])
    async def activations(request: Request, device_id: str | None = None, limit: int = 200) -> list[dict]:
        return await rt_of(request).repo.list_activations(device_id, min(limit, 2000))


    @app.post("/v1/admin/activations/{activation_id}/label", dependencies=[Depends(admin)])
    async def label(activation_id: int, body: LabelIn, request: Request) -> dict:     # FR-2.7
        ok = await rt_of(request).repo.label_activation(activation_id, body.correct)
        if not ok:
            raise HTTPException(404)
        return {"id": activation_id, "label": body.correct}

    return app


def app_factory() -> FastAPI:
    """uvicorn entry: ``uvicorn mitra.main:app_factory --factory``"""
    return create_app()
