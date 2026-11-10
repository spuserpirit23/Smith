from __future__ import annotations

import asyncio
import os
import socket
import textwrap
from pathlib import Path

import httpx
import pytest
import uvicorn

from mitra.config import Settings, load_app_config
from mitra.main import create_app
from mitra.providers.registry import build_providers
from mitra.providers.stt import MockSTT
from mitra.repo import MemoryRepo, PostgresRepo
from mitra.runtime import Runtime

BACKEND = Path(__file__).resolve().parents[1]
ADMIN = "test-admin-token"


def write_test_config(tmp: Path, overrides: str = "") -> Path:
    cfgdir = tmp / "config"
    cfgdir.mkdir(parents=True, exist_ok=True)
    (cfgdir / "system_prompt.md").write_text((BACKEND / "config/system_prompt.md").read_text())
    body = textwrap.dedent(f"""
    persona: {{name: Mitra, language: en-IN, system_prompt_file: config/system_prompt.md, max_reply_sentences: 4,
               greeting: "Hi, I'm Mitra. How are you?"}}
    conversation:
      context_turns: 10
      no_speech_timeout_s: 1.5
      end_of_speech_silence_ms: 600
      max_utterance_s: 8
      max_repeat_prompts: 2
      min_transcript_confidence: 0.45
      follow_up_window_s: 1.0
      hands_free_wait_s: 1.5
      playback_tail_ms: 300
      timeouts: {{stt_s: 2, llm_s: 1.5, tts_s: 2}}
      history: {{persist: true, retention_days: 7}}
    wake: {{provider: mock}}
    stt: {{provider: mock}}
    llm: {{provider: mock}}
    tts: {{providers: mock}}
    vision:
      provider: mock
      min_confidence: 0.6
      capture_timeout_s: 2
      intents: ["what (do|can) you see"]
    storage: {{provider: local, local: {{root: {tmp / 'data'}}}}}
    """) + overrides
    p = cfgdir / "mitra.yaml"
    p.write_text(body)
    return p


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Stack:
    def __init__(self, base: str, rt: Runtime):
        self.base = base
        self.ws_url = base.replace("http", "ws") + "/v1/device/ws"
        self.rt = rt
        self.http = httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {ADMIN}"})

    async def provision(self, device_id: str, **kw) -> str:
        r = await self.http.post("/v1/admin/devices", json={"id": device_id, **kw})
        assert r.status_code == 200, r.text
        return r.json()["secret"]


@pytest.fixture
async def stack(tmp_path, request):
    MockSTT.script.clear()
    overrides = getattr(request, "param", "")
    cfg = load_app_config(write_test_config(tmp_path, overrides))
    dsn = os.environ.get("MITRA_TEST_DATABASE_URL", "")
    repo = PostgresRepo(dsn) if dsn else MemoryRepo()
    port = free_port()
    settings = Settings(admin_token=ADMIN, signing_secret="sign-test", public_base_url=f"http://127.0.0.1:{port}",
                        log_json=False, log_level="WARNING")
    rt = Runtime(settings, cfg, repo, build_providers(cfg))
    app = create_app(settings, rt)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    if dsn:  # clean slate for each test
        async with repo.pool.acquire() as c:
            await c.execute("TRUNCATE devices CASCADE")
    s = Stack(f"http://127.0.0.1:{port}", rt)
    yield s
    await s.http.aclose()
    server.should_exit = True
    await task
