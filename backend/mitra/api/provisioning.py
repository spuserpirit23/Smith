"""Device provisioning helpers shared by the admin API and the CLI."""
from __future__ import annotations

from urllib.parse import urlparse

from ..auth import hash_secret, mosquitto_hash, valid_device_id
from ..repo import Device, new_secret


def make_device(device_id: str, kind: str, name: str = "", paired_main: str | None = None,
                camera_role: str | None = None, history_opt_in: bool = False) -> tuple[Device, str]:
    if not valid_device_id(device_id):
        raise ValueError("device id must be 3-40 chars of a-z, 0-9, '-' and start alphanumeric")
    if kind not in ("main", "camera"):
        raise ValueError("kind must be 'main' or 'camera'")
    if kind == "camera" and not paired_main:
        raise ValueError("cameras need paired_main")
    secret = new_secret()
    dev = Device(id=device_id, kind=kind, name=name, secret_hash=hash_secret(secret),
                 mqtt_hash=mosquitto_hash(secret), paired_main=paired_main,
                 camera_role=camera_role if kind == "camera" else None, history_opt_in=history_opt_in)
    return dev, secret


def nvs_csv(device: Device, secret: str, public_base_url: str) -> str:
    """CSV for ESP-IDF's nvs_partition_gen.py. Flash the resulting .bin into the 'mitra_cfg'
    partition so credentials never live in the firmware image or repo."""
    u = urlparse(public_base_url)
    ws_scheme = "wss" if u.scheme == "https" else "ws"
    host = u.netloc
    rows = [
        "key,type,encoding,value",
        "mitra,namespace,,",
        f"device_id,data,string,{device.id}",
        f"secret,data,string,{secret}",
        f"ws_url,data,string,{ws_scheme}://{host}/v1/device/ws",
        f"mqtt_url,data,string,{ws_scheme}://{host}/mqtt",
        f"api_base,data,string,{public_base_url.rstrip('/')}",
    ]
    return "\n".join(rows) + "\n"
