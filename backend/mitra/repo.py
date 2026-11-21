"""Persistence. Plain SQL on PostgreSQL (runs on any host/cloud), plus an in-memory twin for
tests and quick local runs. Nothing here is AWS-specific.

Privacy defaults (PRD §6): conversation text is stored only when both the global
``conversation.history.persist`` flag and the device's ``history_opt_in`` are true, and always
with an expiry. Metrics rows hold timings and character counts, never transcript text.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

UTC = dt.timezone.utc


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


@dataclass
class Device:
    id: str
    kind: str = "main"                     # main | camera
    name: str = ""
    secret_hash: str = ""                  # sha256 of the device secret (WS auth)
    mqtt_hash: str = ""                    # mosquitto $7$ PBKDF2 hash of the same secret
    enabled: bool = True                   # kill switch
    history_opt_in: bool = False
    config: dict[str, Any] = field(default_factory=dict)    # "desired" (shadow)
    reported: dict[str, Any] = field(default_factory=dict)  # "reported" (shadow)
    paired_main: str | None = None         # cameras: which main board they serve
    camera_role: str | None = None         # cameras: "user" | "room"
    fw_version: str | None = None
    last_seen: dt.datetime | None = None
    created_at: dt.datetime = field(default_factory=now)


METRIC_FIELDS = (
    "speech_end_to_transcript_ms",
    "transcript_to_first_token_ms",
    "first_token_to_first_audio_ms",
    "first_audio_to_speaker_ms",
    "end_to_first_audio_ms",
)


class Repo(Protocol):
    async def init(self) -> None: ...
    async def close(self) -> None: ...
    async def create_device(self, d: Device) -> None: ...
    async def get_device(self, device_id: str) -> Device | None: ...
    async def list_devices(self) -> list[Device]: ...
    async def update_device(self, device_id: str, **fields: Any) -> None: ...
    async def delete_device(self, device_id: str) -> None: ...
    async def add_turn(self, device_id: str, session_id: str, role: str, content: str, ttl_days: int) -> None: ...
    async def recent_turns(self, device_id: str, limit: int) -> list[tuple[str, str]]: ...
    async def delete_history(self, device_id: str) -> int: ...
    async def purge_expired(self) -> int: ...
    async def add_metrics(self, row: dict[str, Any]) -> None: ...
    async def list_metrics(self, device_id: str | None, since: dt.datetime) -> list[dict[str, Any]]: ...
    async def add_activation(self, device_id: str, engine: str, confidence: float) -> int: ...
    async def label_activation(self, activation_id: int, label: bool) -> bool: ...
    async def list_activations(self, device_id: str | None, limit: int) -> list[dict[str, Any]]: ...
    async def delete_device_data(self, device_id: str) -> dict[str, int]: ...


def summarize_latency(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Median and p95 per stage (PRD FR-3.4 latency report)."""
    out: dict[str, Any] = {"turns": len(rows)}
    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r.get("outcome") or "unknown"] = outcomes.get(r.get("outcome") or "unknown", 0) + 1
    out["outcomes"] = outcomes
    for f in METRIC_FIELDS:
        vals = sorted(float(r[f]) for r in rows if r.get(f) is not None)
        if not vals:
            out[f] = None
            continue
        p95 = vals[min(len(vals) - 1, int(round(0.95 * (len(vals) - 1))))]
        out[f] = {"n": len(vals), "median": round(statistics.median(vals), 1), "p95": round(p95, 1)}
    return out


# --------------------------------------------------------------------------------------------
class MemoryRepo:
    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self.turns: list[dict[str, Any]] = []
        self.metrics: list[dict[str, Any]] = []
        self.activations: list[dict[str, Any]] = []

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def create_device(self, d: Device) -> None:
        if d.id in self.devices:
            raise ValueError(f"device {d.id} exists")
        self.devices[d.id] = d

    async def get_device(self, device_id: str) -> Device | None:
        return self.devices.get(device_id)

    async def list_devices(self) -> list[Device]:
        return sorted(self.devices.values(), key=lambda d: d.id)

    async def update_device(self, device_id: str, **fields: Any) -> None:
        d = self.devices[device_id]
        for k, v in fields.items():
            setattr(d, k, v)

    async def delete_device(self, device_id: str) -> None:
        self.devices.pop(device_id, None)

    async def add_turn(self, device_id, session_id, role, content, ttl_days) -> None:
        self.turns.append({"device_id": device_id, "session_id": session_id, "role": role,
                           "content": content, "created_at": now(),
                           "expires_at": now() + dt.timedelta(days=ttl_days)})

    async def recent_turns(self, device_id, limit) -> list[tuple[str, str]]:
        t = [x for x in self.turns if x["device_id"] == device_id and x["expires_at"] > now()]
        return [(x["role"], x["content"]) for x in t[-limit:]]

    async def delete_history(self, device_id) -> int:
        before = len(self.turns)
        self.turns = [x for x in self.turns if x["device_id"] != device_id]
        return before - len(self.turns)

    async def purge_expired(self) -> int:
        before = len(self.turns)
        self.turns = [x for x in self.turns if x["expires_at"] > now()]
        return before - len(self.turns)

    async def add_metrics(self, row) -> None:
        self.metrics.append({"created_at": now(), **row})

    async def list_metrics(self, device_id, since) -> list[dict[str, Any]]:
        return [m for m in self.metrics
                if m["created_at"] >= since and (device_id is None or m["device_id"] == device_id)]

    async def add_activation(self, device_id, engine, confidence) -> int:
        aid = len(self.activations) + 1
        self.activations.append({"id": aid, "device_id": device_id, "engine": engine,
                                 "confidence": confidence, "label": None, "at": now()})
        return aid

    async def label_activation(self, activation_id, label) -> bool:
        for a in self.activations:
            if a["id"] == activation_id:
                a["label"] = label
                return True
        return False

    async def list_activations(self, device_id, limit) -> list[dict[str, Any]]:
        rows = [a for a in self.activations if device_id is None or a["device_id"] == device_id]
        return list(reversed(rows))[:limit]

    async def delete_device_data(self, device_id) -> dict[str, int]:
        h = await self.delete_history(device_id)
        m0, a0 = len(self.metrics), len(self.activations)
        self.metrics = [m for m in self.metrics if m["device_id"] != device_id]
        self.activations = [a for a in self.activations if a["device_id"] != device_id]
        return {"turns": h, "metrics": m0 - len(self.metrics), "activations": a0 - len(self.activations)}


# --------------------------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'main',
  name TEXT NOT NULL DEFAULT '',
  secret_hash TEXT NOT NULL,
  mqtt_hash TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  history_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
  config JSONB NOT NULL DEFAULT '{}',
  reported JSONB NOT NULL DEFAULT '{}',
  paired_main TEXT,
  camera_role TEXT,
  fw_version TEXT,
  last_seen TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS turns (
  id BIGSERIAL PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_device_idx ON turns (device_id, created_at);
CREATE INDEX IF NOT EXISTS turns_expiry_idx ON turns (expires_at);
CREATE TABLE IF NOT EXISTS turn_metrics (
  id BIGSERIAL PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  data JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS turn_metrics_idx ON turn_metrics (device_id, created_at);
CREATE TABLE IF NOT EXISTS activations (
  id BIGSERIAL PRIMARY KEY,
  device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  at TIMESTAMPTZ NOT NULL DEFAULT now(),
  engine TEXT NOT NULL,
  confidence REAL NOT NULL,
  label BOOLEAN
);
CREATE INDEX IF NOT EXISTS activations_idx ON activations (device_id, at);
"""

_DEVICE_COLS = [f for f in Device.__dataclass_fields__]


class PostgresRepo:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def init(self) -> None:
        import asyncpg

        async def setup(conn):
            await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")

        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5, init=setup)
        async with self.pool.acquire() as c:
            await c.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    @staticmethod
    def _row_to_device(r) -> Device:
        return Device(**{k: r[k] for k in _DEVICE_COLS})

    async def create_device(self, d: Device) -> None:
        vals = asdict(d)
        cols = ", ".join(_DEVICE_COLS)
        ph = ", ".join(f"${i + 1}" for i in range(len(_DEVICE_COLS)))
        import asyncpg

        try:
            await self.pool.execute(f"INSERT INTO devices ({cols}) VALUES ({ph})", *[vals[c] for c in _DEVICE_COLS])
        except asyncpg.UniqueViolationError as e:
            raise ValueError(f"device {d.id} exists") from e

    async def get_device(self, device_id: str) -> Device | None:
        r = await self.pool.fetchrow("SELECT * FROM devices WHERE id=$1", device_id)
        return self._row_to_device(r) if r else None

    async def list_devices(self) -> list[Device]:
        return [self._row_to_device(r) for r in await self.pool.fetch("SELECT * FROM devices ORDER BY id")]

    async def update_device(self, device_id: str, **fields: Any) -> None:
        bad = set(fields) - set(_DEVICE_COLS)
        if bad:
            raise ValueError(f"unknown fields {bad}")
        sets = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(fields))
        await self.pool.execute(f"UPDATE devices SET {sets} WHERE id=$1", device_id, *fields.values())

    async def delete_device(self, device_id: str) -> None:
        await self.pool.execute("DELETE FROM devices WHERE id=$1", device_id)

    async def add_turn(self, device_id, session_id, role, content, ttl_days) -> None:
        await self.pool.execute(
            "INSERT INTO turns (device_id, session_id, role, content, expires_at) "
            "VALUES ($1,$2,$3,$4, now() + make_interval(days => $5))",
            device_id, session_id, role, content, ttl_days,
        )

    async def recent_turns(self, device_id, limit) -> list[tuple[str, str]]:
        rows = await self.pool.fetch(
            "SELECT role, content FROM (SELECT role, content, id FROM turns WHERE device_id=$1 "
            "AND expires_at > now() ORDER BY id DESC LIMIT $2) t ORDER BY id",
            device_id, limit,
        )
        return [(r["role"], r["content"]) for r in rows]

    async def delete_history(self, device_id) -> int:
        res = await self.pool.execute("DELETE FROM turns WHERE device_id=$1", device_id)
        return int(res.split()[-1])

    async def purge_expired(self) -> int:
        res = await self.pool.execute("DELETE FROM turns WHERE expires_at <= now()")
        return int(res.split()[-1])

    async def add_metrics(self, row) -> None:
        data = {k: v for k, v in row.items() if k != "device_id"}
        await self.pool.execute("INSERT INTO turn_metrics (device_id, data) VALUES ($1,$2)", row["device_id"], data)

    async def list_metrics(self, device_id, since) -> list[dict[str, Any]]:
        if device_id:
            rows = await self.pool.fetch(
                "SELECT device_id, created_at, data FROM turn_metrics WHERE device_id=$1 AND created_at >= $2",
                device_id, since)
        else:
            rows = await self.pool.fetch(
                "SELECT device_id, created_at, data FROM turn_metrics WHERE created_at >= $1", since)
        return [{"device_id": r["device_id"], "created_at": r["created_at"], **r["data"]} for r in rows]

    async def add_activation(self, device_id, engine, confidence) -> int:
        return await self.pool.fetchval(
            "INSERT INTO activations (device_id, engine, confidence) VALUES ($1,$2,$3) RETURNING id",
            device_id, engine, confidence)

    async def label_activation(self, activation_id, label) -> bool:
        res = await self.pool.execute("UPDATE activations SET label=$2 WHERE id=$1", activation_id, label)
        return res.endswith("1")

    async def list_activations(self, device_id, limit) -> list[dict[str, Any]]:
        if device_id:
            rows = await self.pool.fetch(
                "SELECT * FROM activations WHERE device_id=$1 ORDER BY id DESC LIMIT $2", device_id, limit)
        else:
            rows = await self.pool.fetch("SELECT * FROM activations ORDER BY id DESC LIMIT $1", limit)
        return [dict(r) for r in rows]

    async def delete_device_data(self, device_id) -> dict[str, int]:
        async with self.pool.acquire() as c, c.transaction():
            t = await c.execute("DELETE FROM turns WHERE device_id=$1", device_id)
            m = await c.execute("DELETE FROM turn_metrics WHERE device_id=$1", device_id)
            a = await c.execute("DELETE FROM activations WHERE device_id=$1", device_id)
        return {"turns": int(t.split()[-1]), "metrics": int(m.split()[-1]), "activations": int(a.split()[-1])}


class FileRepo(MemoryRepo):
    """MemoryRepo whose *devices* survive restarts, saved as JSON (local single-machine use).

    ``DATABASE_URL=file:/path/devices.json``. Only device records are persisted (secret hashes,
    never secrets); conversation turns, metrics and activations stay in memory as before.
    """

    _DATES = ("last_seen", "created_at")

    def __init__(self, path: str) -> None:
        super().__init__()
        from pathlib import Path

        self.path = Path(path)

    async def init(self) -> None:
        if not self.path.exists():
            return
        for raw in json.loads(self.path.read_text(encoding="utf-8")):
            for k in self._DATES:
                if raw.get(k):
                    raw[k] = dt.datetime.fromisoformat(raw[k])
            d = Device(**raw)
            self.devices[d.id] = d

    def _save(self) -> None:
        rows = []
        for d in self.devices.values():
            row = asdict(d)
            for k in self._DATES:
                if row.get(k):
                    row[k] = row[k].isoformat()
            rows.append(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        tmp.replace(self.path)   # atomic: a crash mid-write never leaves a half file

    async def create_device(self, d: Device) -> None:
        await super().create_device(d)
        self._save()

    async def update_device(self, device_id: str, **fields: Any) -> None:
        await super().update_device(device_id, **fields)
        self._save()

    async def delete_device(self, device_id: str) -> None:
        await super().delete_device(device_id)
        self._save()


def make_repo(dsn: str) -> Repo:
    if dsn.startswith("file:"):
        return FileRepo(dsn[len("file:"):])
    return PostgresRepo(dsn) if dsn else MemoryRepo()


def new_secret() -> str:
    return secrets.token_urlsafe(24)
