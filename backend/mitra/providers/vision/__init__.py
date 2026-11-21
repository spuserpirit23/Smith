"""Vision providers for camera option A (scene description) and B (presence)."""
from __future__ import annotations

import asyncio
import base64
import json
import re

import httpx

from ..base import ProviderError, VisionResult

VISION_PROMPT = """You are the eyes of a small companion robot. Look at the photo and answer the user's \
question: "{question}".
Rules: describe only what is clearly visible; never identify or guess who any person is, their name, \
age, ethnicity, or health; mention if the image is dark, blurry, or unclear.
Reply with JSON only, no prose around it:
{{"description": "<one or two short spoken-style sentences>", "confidence": <0.0-1.0 how clear the \
image and your answer are>, "person_present": <true|false>}}"""


# Small models (moondream) garble their first word when told to "answer in one short sentence",
# but answer cleanly to an open question; we trim the answer ourselves instead.
PLAIN_VISION_PROMPT = "What is in this image?"

_PERSON_WORDS = re.compile(r"\b(person|people|man|men|woman|women|boy|girl|child|children|kid|kids|"
                           r"baby|someone|face|figure)\b", re.I)
_OPENERS = re.compile(r"^(the|this) (image|photo|picture|scene) (shows|features|depicts|presents|"
                      r"captures|displays|contains|is of)\s+", re.I)


def parse_plain_vision(text: str) -> VisionResult:
    """For small vision models that can't follow the JSON format: keep the first sentence or two,
    phrased as the robot speaking ("I can see ...")."""
    t = " ".join((text or "").split())
    if t and t[0].islower() and " " in t:   # dropped first characters ("urn of a house ...")
        t = t.split(" ", 1)[1]
    sentences = re.split(r"(?<=[.!?])\s+", t)
    t = " ".join(sentences[:2]).strip()
    if len(t) > 240:
        t = t[:240].rsplit(" ", 1)[0].rstrip(",;:") + "."
    t = _OPENERS.sub("I can see ", t)
    t = t[:1].upper() + t[1:]
    if not t:
        return VisionResult("I can't make out anything clearly.", 0.0, [], None)
    return VisionResult(t, 0.7, [], bool(_PERSON_WORDS.search(t)))


def parse_vision_json(text: str) -> VisionResult:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return VisionResult(description=text.strip()[:300], confidence=0.5)
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return VisionResult(description=text.strip()[:300], confidence=0.5)
    return VisionResult(
        description=str(obj.get("description", "")).strip(),
        confidence=float(obj.get("confidence", 0.5) or 0.0),
        person_present=obj.get("person_present"),
    )


class BedrockVision:
    name = "bedrock"

    def __init__(self, region: str, model_id: str):
        import boto3

        self._client = boto3.client("bedrock-runtime", region_name=region)
        self.model_id = model_id

    async def describe(self, jpeg: bytes, question: str) -> VisionResult:
        def call() -> str:
            resp = self._client.converse(
                modelId=self.model_id,
                messages=[{
                    "role": "user",
                    "content": [
                        {"image": {"format": "jpeg", "source": {"bytes": jpeg}}},
                        {"text": VISION_PROMPT.format(question=question)},
                    ],
                }],
                inferenceConfig={"maxTokens": 200, "temperature": 0.2},
            )
            return "".join(b.get("text", "") for b in resp["output"]["message"]["content"])

        try:
            return parse_vision_json(await asyncio.to_thread(call))
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"bedrock vision failed: {e}") from e


class AnthropicVision:
    name = "anthropic"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(timeout=20)

    async def describe(self, jpeg: bytes, question: str) -> VisionResult:
        body = {
            "model": self.model,
            "max_tokens": 200,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.b64encode(jpeg).decode()}},
                    {"type": "text", "text": VISION_PROMPT.format(question=question)},
                ],
            }],
        }
        try:
            r = await self._client.post(
                "https://api.anthropic.com/v1/messages", json=body,
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            )
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json()["content"])
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"anthropic vision failed: {e}") from e
        return parse_vision_json(text)


class OpenAICompatVision:
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str, model: str, reasoning_effort: str = "",
                 prompt_style: str = "json", timeout_s: float = 20):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.plain = prompt_style == "plain"   # small models: plain description, no JSON
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def describe(self, jpeg: bytes, question: str) -> VisionResult:
        data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        body = {
            "model": self.model,
            "max_tokens": 200,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": PLAIN_VISION_PROMPT if self.plain else VISION_PROMPT.format(question=question)},
                ],
            }],
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if self.plain:
            body["temperature"] = 0      # small models ramble less and stay consistent
            body["max_tokens"] = 90
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = await self._client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"vision http failed: {e!r}") from e
        return parse_plain_vision(text) if self.plain else parse_vision_json(text)


class RekognitionVision:
    """Labels only (cheapest AWS option); description is assembled from top labels."""

    name = "rekognition"

    def __init__(self, region: str):
        import boto3

        self._client = boto3.client("rekognition", region_name=region)

    async def describe(self, jpeg: bytes, question: str) -> VisionResult:
        def call():
            return self._client.detect_labels(Image={"Bytes": jpeg}, MaxLabels=10, MinConfidence=60)

        try:
            resp = await asyncio.to_thread(call)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"rekognition failed: {e}") from e
        labels = [(lbl["Name"], lbl["Confidence"] / 100) for lbl in resp.get("Labels", [])]
        person = any(n.lower() == "person" for n, _ in labels)
        if not labels:
            return VisionResult("I can't make out anything clearly.", 0.0, [], person)
        names = [n.lower() for n, _ in labels[:4]]
        desc = "I can see " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0])
        return VisionResult(desc + ".", labels[0][1], labels, person)


class MockVision:
    name = "mock"

    def __init__(self, confidence: float = 0.9):
        self.confidence = confidence

    async def describe(self, jpeg: bytes, question: str) -> VisionResult:
        if not jpeg.startswith(b"\xff\xd8"):
            raise ProviderError("not a jpeg")
        return VisionResult("A desk with a laptop and a cup of tea.", self.confidence,
                            [("desk", 0.9), ("laptop", 0.88), ("cup", 0.8)], False)
