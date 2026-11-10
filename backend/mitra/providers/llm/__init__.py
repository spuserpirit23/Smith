"""LLM providers. All stream text deltas."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, AsyncIterator, Iterator

import httpx

from ..base import ChatMessage, ProviderError

log = logging.getLogger(__name__)


def normalize(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Merge consecutive same-role messages and make sure the list starts with a user turn."""
    out: list[ChatMessage] = []
    for m in messages:
        if out and out[-1].role == m.role:
            out[-1] = ChatMessage(m.role, out[-1].content + "\n" + m.content)
        else:
            out.append(ChatMessage(m.role, m.content))
    while out and out[0].role != "user":
        out.pop(0)
    return out


async def iterate_in_thread(make_iter) -> AsyncIterator[Any]:
    """Run a blocking iterator (e.g. boto3 event stream) in a thread, yield items async."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    stop = threading.Event()

    def worker() -> None:
        try:
            it: Iterator[Any] = make_iter()
            for item in it:
                if stop.is_set():
                    break
                loop.call_soon_threadsafe(queue.put_nowait, ("item", item))
            loop.call_soon_threadsafe(queue.put_nowait, ("done", None))
        except Exception as e:  # noqa: BLE001
            loop.call_soon_threadsafe(queue.put_nowait, ("error", e))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        while True:
            kind, val = await queue.get()
            if kind == "item":
                yield val
            elif kind == "done":
                return
            else:
                raise ProviderError(str(val)) from val
    finally:
        stop.set()


class BedrockLLM:
    """Amazon Bedrock Converse API (model-agnostic: Claude, Llama, Mistral, Nova...)."""

    name = "bedrock"

    def __init__(self, region: str, model_id: str, max_tokens: int = 300, temperature: float = 0.6):
        try:
            import boto3
        except ImportError as e:  # pragma: no cover
            raise ProviderError("boto3 not installed (pip install '.[aws]')") from e
        self._client = boto3.client("bedrock-runtime", region_name=region)
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature

    async def stream(self, system: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        msgs = [{"role": m.role, "content": [{"text": m.content}]} for m in normalize(messages)]

        def make_iter():
            resp = self._client.converse_stream(
                modelId=self.model_id,
                system=[{"text": system}],
                messages=msgs,
                inferenceConfig={"maxTokens": self.max_tokens, "temperature": self.temperature},
            )
            for event in resp["stream"]:
                delta = event.get("contentBlockDelta", {}).get("delta", {})
                if "text" in delta:
                    yield delta["text"]

        async for text in iterate_in_thread(make_iter):
            yield text


async def _sse_lines(resp: httpx.Response) -> AsyncIterator[dict]:
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


class AnthropicLLM:
    """Anthropic Messages API directly (same models as Bedrock, no AWS dependency)."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 300, temperature: float = 0.6):
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY is empty")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5))

    async def stream(self, system: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system,
            "stream": True,
            "messages": [{"role": m.role, "content": m.content} for m in normalize(messages)],
        }
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        try:
            async with self._client.stream(
                "POST", "https://api.anthropic.com/v1/messages", json=body, headers=headers
            ) as r:
                if r.status_code >= 400:
                    raise ProviderError(f"anthropic http {r.status_code}: {(await r.aread())[:200]!r}")
                async for ev in _sse_lines(r):
                    if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                        yield ev["delta"]["text"]
        except httpx.HTTPError as e:
            raise ProviderError(f"anthropic stream failed: {e}") from e


class ThinkFilter:
    """Drop ``<think>...</think>`` spans from a token stream (Qwen3, DeepSeek-R1 and other local
    reasoning models served raw by llama.cpp / older Ollama). Robot speech must never read them out.
    Tags may be split across deltas, so a short tail is held back until it can't be a tag prefix."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.inside = False
        self.started = False  # strip whitespace left between </think> and the real reply
        # A "</think>" with no "<think>" means the chat template pre-filled the open tag (thinking-only
        # models such as qwen3:4b / *-thinking-2507): everything before it was reasoning and has
        # already been spoken. Can't be undone mid-stream; flagged so the caller can log it.
        self.leaked = False

    def feed(self, delta: str) -> str:
        self.buf += delta
        out = []
        while self.buf:
            tag = self.CLOSE if self.inside else self.OPEN
            if not self.inside and (j := self.buf.find(self.CLOSE)) >= 0 and \
                    (self.buf.find(self.OPEN) < 0 or j < self.buf.find(self.OPEN)):
                out.append(self.buf[:j])
                self.buf = self.buf[j + len(self.CLOSE):]
                self.leaked = True
                continue
            i = self.buf.find(tag)
            if i >= 0:
                if not self.inside:
                    out.append(self.buf[:i])
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            tags = (self.CLOSE,) if self.inside else (self.OPEN, self.CLOSE)
            keep = next((k for k in range(min(len(self.CLOSE) - 1, len(self.buf)), 0, -1)
                         if any(t.startswith(self.buf[-k:]) for t in tags)), 0)
            if not self.inside:
                out.append(self.buf[: len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return self._mark("".join(out))

    def _mark(self, text: str) -> str:
        if not self.started:
            text = text.lstrip()
            self.started = bool(text)
        return text

    def flush(self) -> str:
        rest, self.buf = ("" if self.inside else self.buf), ""
        return self._mark(rest)


class OpenAICompatLLM:
    """/chat/completions streaming — a local Ollama / llama.cpp / vLLM server (the default, no
    per-token cost), or any hosted OpenAI-compatible API (OpenAI, Groq, OpenRouter, Gemini)."""

    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, max_tokens: int = 300, temperature: float = 0.6,
                 reasoning_effort: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        # Thinking models (Gemini 2.5+, o-series) spend max_tokens on reasoning; "none"/"low" keeps
        # spoken replies fast and non-empty. Empty = don't send the field.
        self.reasoning_effort = reasoning_effort
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5))

    async def stream(self, system: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        body = {
            "model": self.model,
            "stream": True,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in normalize(messages)],
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        think = ThinkFilter()
        try:
            async with self._client.stream(
                "POST", f"{self.base_url}/chat/completions", json=body, headers=headers
            ) as r:
                if r.status_code >= 400:
                    raise ProviderError(f"llm http {r.status_code}: {(await r.aread())[:200]!r}")
                async for ev in _sse_lines(r):
                    choices = ev.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta", {}).get("content")
                        if delta and (text := think.feed(delta)):
                            yield text
                if tail := think.flush():
                    yield tail
                if think.leaked:
                    log.error("llm streamed reasoning as reply text: model %r is thinking-only; use a "
                              "non-thinking/instruct model (see docs/LOCAL_MODELS.md)", self.model)
        except httpx.HTTPError as e:
            raise ProviderError(f"llm stream failed: {e}") from e

    async def warm_up(self, system: str) -> None:
        """Load the model and cache the system-prompt prefix before the first real turn.

        A local model's cold load (5-40 s on CPU) would otherwise land on the first user and blow
        the LLM timeout. Also surfaces a thinking-only model (logged by ``stream``) at boot.
        """
        t0 = time.monotonic()
        reply = ""
        async for delta in self.stream(system, [ChatMessage("user", "Hello!")]):
            reply += delta
        log.info("llm warm-up done", extra={"model": self.model, "secs": round(time.monotonic() - t0, 1),
                                            "reply_chars": len(reply)})


class MockLLM:
    """Echoes the user, streamed word by word. Special inputs drive fault-injection tests:
    'trigger llm timeout' hangs, 'trigger llm error' raises."""

    name = "mock"

    def __init__(self, delay_s: float = 0.01):
        self.delay_s = delay_s
        self.last_system: str | None = None
        self.last_messages: list[ChatMessage] = []

    async def stream(self, system: str, messages: list[ChatMessage]) -> AsyncIterator[str]:
        self.last_system = system
        self.last_messages = list(messages)
        user = messages[-1].content if messages else ""
        if "trigger llm timeout" in user.lower():
            await asyncio.sleep(3600)
        if "trigger llm error" in user.lower():
            raise ProviderError("injected llm failure")
        reply = f"You said: {user.strip()}. That's lovely to hear! What else is on your mind?"
        for word in reply.split(" "):
            await asyncio.sleep(self.delay_s)
            yield word + " "
