
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MITRA_", env_file=".env", extra="ignore")

    config_file: str = "config/mitra.yaml"
    # Empty DATABASE_URL -> in-memory repository (dev/tests only; data lost on restart).
    database_url: str = ""
    mqtt_host: str = ""                      # empty -> MQTT disabled (camera + shadow features off)
    mqtt_port: int = 1883
    mqtt_username: str = "mitra-backend"
    mqtt_password: str = ""
    mosquitto_passwd_file: str = ""          # backend regenerates this file from the device table
    admin_token: str = Field(default="", description="Bearer token for /v1/admin/*")
    signing_secret: str = Field(default="", description="HMAC key for short-lived upload/OTA URLs")
    public_base_url: str = "http://localhost:8080"
    log_level: str = "INFO"
    log_json: bool = True


def _interpolate(value: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        env = os.environ.get(name)
        if env is None or env == "":
            return default if default is not None else ""
        return env

    return _ENV_PATTERN.sub(repl, value)


def _resolve_ssm(value: str, cache: dict[str, str]) -> str:
    if not value.startswith("ssm:"):
        return value
    path = value[4:]
    if path not in cache:
        import boto3  # optional dependency, only needed when ssm: refs are used

        client = boto3.client("ssm")
        cache[path] = client.get_parameter(Name=path, WithDecryption=True)["Parameter"]["Value"]
    return cache[path]


def _walk(node: Any, cache: dict[str, str]) -> Any:
    if isinstance(node, dict):
        return {k: _walk(v, cache) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, cache) for v in node]
    if isinstance(node, str):
        return _resolve_ssm(_interpolate(node), cache)
    return node


class AppConfig:
    """Thin dict wrapper with dotted access: cfg.get("conversation.timeouts.stt_s")."""

    def __init__(self, data: dict[str, Any], base_dir: Path):
        self.data = data
        self.base_dir = base_dir

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, dotted: str) -> dict[str, Any]:
        val = self.get(dotted, {})
        return dict(val) if isinstance(val, dict) else {}

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.base_dir / p

    def system_prompt(self) -> str:
        persona = self.section("persona")
        template = self.path(persona.get("system_prompt_file", "config/system_prompt.md")).read_text()
        return template.format(
            name=persona.get("name", "Mitra"),
            language=persona.get("language", "en-IN"),
            max_reply_sentences=persona.get("max_reply_sentences", 4),
        )


def load_app_config(path: str | os.PathLike[str]) -> AppConfig:
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    raw = yaml.safe_load(p.read_text()) or {}
    data = _walk(raw, {})
    # config/ lives one level below the backend root; prompt paths are relative to that root.
    return AppConfig(data, base_dir=p.parent.parent)


@lru_cache
def get_settings() -> Settings:
    return Settings()
