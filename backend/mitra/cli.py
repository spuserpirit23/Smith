"""Operator CLI. Talks to the running backend over its admin API, so it works the same against a
local stack, the EC2 box, or any other host.

  mitra provision mitra-001                          # main board
  mitra provision cam-001 --kind camera --paired-main mitra-001 --role user
  mitra devices
  mitra delete-history mitra-001                     # FR-4.3
  mitra delete-data mitra-001                        # history + metrics + activations + frames
  mitra disable mitra-001 / mitra enable mitra-001   # kill switch
  mitra latency [--device mitra-001] [--hours 24]    # FR-3.4 report
  mitra activations --device mitra-001
  mitra label 42 --false                             # FR-2.7 wake-word test labelling
  mitra ota mitra-001 build/mitra.bin 0.2.0
  mitra render-clips ../firmware/main-board/main/clips   # local fallback phrases via configured TTS
  mitra check-providers                              # one LLM + vision call (reads .env), no server

Env: MITRA_API (default http://localhost:8080), MITRA_ADMIN_TOKEN.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

FALLBACK_CLIPS = {
    "no_brain": "Sorry, I can't reach my brain right now. Please try again in a little while.",
    "repeat": "Sorry, could you say that again?",
    "no_wifi": "I can't connect to the internet right now.",
    "standby": "Okay, I'll be here if you need me.",
}


def _client() -> httpx.Client:
    token = os.environ.get("MITRA_ADMIN_TOKEN", "")
    if not token:
        sys.exit("MITRA_ADMIN_TOKEN is not set")
    return httpx.Client(base_url=os.environ.get("MITRA_API", "http://localhost:8080"),
                        headers={"Authorization": f"Bearer {token}"}, timeout=60)


def _print(r: httpx.Response) -> None:
    if r.status_code >= 400:
        sys.exit(f"HTTP {r.status_code}: {r.text}")
    ctype = r.headers.get("content-type", "")
    print(json.dumps(r.json(), indent=2, default=str) if "json" in ctype else r.text)


def cmd_provision(a) -> None:
    with _client() as c:
        r = c.post("/v1/admin/devices", json={"id": a.id, "kind": a.kind, "name": a.name or "",
                                               "paired_main": a.paired_main, "camera_role": a.role,
                                               "history_opt_in": a.history_opt_in})
        if r.status_code >= 400:
            sys.exit(f"HTTP {r.status_code}: {r.text}")
        body = r.json()
    out = Path(a.out or f"{a.id}.nvs.csv")
    out.write_text(body["nvs_csv"])
    os.chmod(out, 0o600)
    print(f"Provisioned {body['id']} ({body['kind']}). Credentials written to {out} (mode 600).")
    print("Flash with:  tools/flash_credentials.sh", out, "<serial-port>")
    print("Then delete the CSV. The secret is not recoverable; re-provision to rotate it.")


def cmd_render_clips(a) -> None:
    """Render the on-device fallback phrases with the configured TTS (FR: local fallback in flash)."""
    from .config import load_app_config
    from .providers.registry import build_tts

    cfg = load_app_config(os.environ.get("MITRA_CONFIG_FILE", "config/mitra.yaml"))
    tts = build_tts(cfg)
    lang = cfg.get("persona.language", "en-IN")
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    async def run() -> None:
        for name, text in FALLBACK_CLIPS.items():
            pcm = b"".join([c async for c in tts.synthesize(text, lang)])
            (out_dir / f"{name}.pcm").write_bytes(pcm)
            print(f"{name}.pcm  {len(pcm) / 32000:.2f}s  via {tts.last_used}")

    asyncio.run(run())


def cmd_check_providers(a) -> None:
    """Call the configured LLM (and vision, with a webcam frame or --image) once, outside the server."""
    import time

    from dotenv import load_dotenv

    load_dotenv(".env")
    from .config import load_app_config
    from .providers.base import ChatMessage
    from .providers.registry import build_llm, build_vision

    cfg = load_app_config(os.environ.get("MITRA_CONFIG_FILE", "config/mitra.yaml"))

    async def run() -> None:
        llm = build_llm(cfg)
        print(f"LLM: {llm.name} {getattr(llm, 'model', '')}")
        t0, first, out = time.monotonic(), None, []
        async for d in llm.stream(cfg.system_prompt(), [ChatMessage("user", a.say)]):
            first = first or time.monotonic()
            out.append(d)
        print(f"  first token {((first or t0) - t0) * 1000:.0f} ms, total {(time.monotonic() - t0) * 1000:.0f} ms")
        print(f"  reply: {''.join(out).strip()!r}")
        if a.no_vision:
            return
        vision = build_vision(cfg)
        if vision is None:
            print("vision: disabled")
            return
        if a.image:
            jpg = Path(a.image).read_bytes()
        else:
            from .pc import Webcam
            cam = Webcam(a.camera)
            jpg = cam.snap()
            if cam.dark_hint:
                print(cam.dark_hint)
        print(f"vision: {vision.name} {getattr(vision, 'model', '')} ({len(jpg) // 1024} KB image)")
        t0 = time.monotonic()
        r = await vision.describe(jpg, "What do you see?")
        print(f"  {(time.monotonic() - t0) * 1000:.0f} ms  confidence={r.confidence}  person={r.person_present}")
        print(f"  {r.description!r}")

    asyncio.run(run())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="mitra")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("provision")
    p.add_argument("id")
    p.add_argument("--kind", default="main", choices=["main", "camera"])
    p.add_argument("--name")
    p.add_argument("--paired-main")
    p.add_argument("--role", choices=["user", "room"])
    p.add_argument("--history-opt-in", action="store_true")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_provision)

    sub.add_parser("devices").set_defaults(fn=lambda a: _print(_client().get("/v1/admin/devices")))
    for name, method, path in [("delete-history", "DELETE", "/history"), ("delete-data", "DELETE", "/data"),
                               ("deprovision", "DELETE", "")]:
        q = sub.add_parser(name)
        q.add_argument("id")
        q.set_defaults(fn=lambda a, m=method, s=path: _print(_client().request(m, f"/v1/admin/devices/{a.id}{s}")))
    for name, val in [("disable", False), ("enable", True)]:
        q = sub.add_parser(name)
        q.add_argument("id")
        q.set_defaults(fn=lambda a, v=val: _print(_client().patch(f"/v1/admin/devices/{a.id}", json={"enabled": v})))

    q = sub.add_parser("latency")
    q.add_argument("--device")
    q.add_argument("--hours", type=float, default=24)
    q.set_defaults(fn=lambda a: _print(_client().get("/v1/admin/latency",
                                                     params={k: v for k, v in {"device_id": a.device, "hours": a.hours}.items() if v})))
    q = sub.add_parser("activations")
    q.add_argument("--device")
    q.set_defaults(fn=lambda a: _print(_client().get("/v1/admin/activations",
                                                     params={"device_id": a.device} if a.device else {})))
    q = sub.add_parser("label")
    q.add_argument("activation_id", type=int)
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--true", dest="correct", action="store_true")
    g.add_argument("--false", dest="correct", action="store_false")
    q.set_defaults(fn=lambda a: _print(_client().post(f"/v1/admin/activations/{a.activation_id}/label",
                                                      json={"correct": a.correct})))
    q = sub.add_parser("look")
    q.add_argument("id")
    q.set_defaults(fn=lambda a: _print(_client().post(f"/v1/admin/devices/{a.id}/look")))
    q = sub.add_parser("ota")
    q.add_argument("id")
    q.add_argument("firmware")
    q.add_argument("version")
    q.set_defaults(fn=lambda a: _print(_client().post(
        f"/v1/admin/devices/{a.id}/ota", data={"version": a.version},
        files={"firmware": (Path(a.firmware).name, Path(a.firmware).read_bytes(), "application/octet-stream")})))
    q = sub.add_parser("render-clips")
    q.add_argument("out_dir")
    q.set_defaults(fn=cmd_render_clips)

    q = sub.add_parser("check-providers", help="one LLM + vision call with the .env config")
    q.add_argument("--say", default="Hi Mitra! Tell me a fun fact about elephants.")
    q.add_argument("--image", help="JPEG to describe instead of a webcam frame")
    q.add_argument("--camera", type=int, default=0)
    q.add_argument("--no-vision", action="store_true")
    q.set_defaults(fn=cmd_check_providers)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
