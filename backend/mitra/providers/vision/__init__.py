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

    def __init__(self, base_url: str, api_key: str, model: str, reasoning_effort: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._client = httpx.AsyncClient(timeout=20)

    async def describe(self, jpeg: bytes, question: str) -> VisionResult:
        data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        body = {
            "model": self.model,
            "max_tokens": 200,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": VISION_PROMPT.format(question=question)},
                ],
            }],
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = await self._client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"vision http failed: {e}") from e
        return parse_vision_json(text)


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
