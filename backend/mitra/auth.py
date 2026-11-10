"""Device and operator auth primitives (replaces AWS IoT Core X.509 identities).

Each device gets one random secret at provisioning. It authenticates the device's WebSocket
(``Authorization: Bearer``) and its MQTT login (Mosquitto password file, PBKDF2-SHA512).
The server stores only hashes. Secrets reach the device through an NVS partition image, never
through the firmware repo (PRD §4.2 "No secrets in code or firmware repos").
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time

DEVICE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")


def valid_device_id(device_id: str) -> bool:
    # No '/', '+', '#': device ids are used verbatim in MQTT topics and ACL patterns.
    return bool(DEVICE_ID_RE.match(device_id))


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def check_secret(secret: str, secret_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), secret_hash)


def mosquitto_hash(password: str, iterations: int = 101) -> str:
    """Hash in the format ``mosquitto_passwd`` writes for Mosquitto 2.x ($7$ = PBKDF2-SHA512)."""
    salt = os.urandom(12)
    dk = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations, dklen=64)
    return f"$7${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_mosquitto_hash(password: str, encoded: str) -> bool:
    _, seven, iters, salt_b64, dk_b64 = encoded.split("$")
    assert seven == "7"
    dk = hashlib.pbkdf2_hmac("sha512", password.encode(), base64.b64decode(salt_b64), int(iters), dklen=64)
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


def sign(secret: str, purpose: str, subject: str, ttl_s: int) -> str:
    """Short-lived capability token, e.g. for a camera's one-shot JPEG upload URL."""
    exp = int(time.time()) + ttl_s
    msg = f"{purpose}:{subject}:{exp}".encode()
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()[:18]).decode()
    return f"{exp}.{sig}"


def verify(secret: str, purpose: str, subject: str, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    msg = f"{purpose}:{subject}:{exp}".encode()
    expected = base64.urlsafe_b64encode(hmac.new(secret.encode(), msg, hashlib.sha256).digest()[:18]).decode()
    return hmac.compare_digest(expected, sig)


def check_admin(header: str | None, admin_token: str) -> bool:
    if not admin_token or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:], admin_token)
