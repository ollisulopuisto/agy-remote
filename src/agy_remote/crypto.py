"""Authenticated payload encryption (AES-256-GCM) for agy-remote.

The browser and the server share a 256-bit key that is delivered out of band in
the URL hash fragment. Every WebSocket frame in both directions is sealed with
AES-GCM, so the payload stays confidential even where the transport itself is
cleartext (plain LAN Wi-Fi, or the last hop behind a Tailscale subnet router).

This is pre-shared-key payload encryption between the browser and the server.
It is deliberately *not* described as zero-knowledge end-to-end encryption: the
server generates the key and is one of the two endpoints.

Envelope format (v1)::

    {"encrypted": true, "v": 1, "ts": <unix seconds>,
     "nonce": <b64 96-bit>, "data": <b64 ciphertext||tag>}

``ts`` is bound into the GCM additional-authenticated-data, so it cannot be
slid forward by an attacker without invalidating the tag. Together with the
:class:`ReplayGuard` nonce cache this stops a captured frame (for example a
``send_prompt`` carrying a shell command) from being replayed onto the agent.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

E2EE_VERSION = 1
KEY_BYTES = 32
NONCE_BYTES = 12
#: How far apart the phone's and the host's clocks may drift, in seconds.
DEFAULT_MAX_AGE_SECONDS = 300


class EnvelopeError(ValueError):
    """Base class for malformed or unacceptable envelopes."""


class StaleEnvelopeError(EnvelopeError):
    """Envelope timestamp is outside the freshness window."""


class ReplayError(EnvelopeError):
    """Envelope nonce has already been accepted."""


def generate_e2ee_key() -> str:
    """Generate a 256-bit (32-byte) URL-safe base64 AES key."""
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode("utf-8")


def decode_key(key_str: str) -> bytes:
    """Decode a URL-safe base64 key into raw bytes.

    Raises:
        ValueError: if the key is not exactly 256 bits.
    """
    padded = key_str + "=" * ((4 - len(key_str) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except Exception as e:
        raise ValueError(f"E2EE key is not valid base64: {e}") from e
    if len(raw) != KEY_BYTES:
        raise ValueError(f"E2EE key must be {KEY_BYTES} bytes, got {len(raw)}")
    return raw


def _aad(ts: int) -> bytes:
    """Additional authenticated data binding the version and timestamp."""
    return f"agy-remote/v{E2EE_VERSION}/{ts}".encode()


class ReplayGuard:
    """Remembers recently seen nonces so a captured frame cannot be replayed.

    The cache only needs to cover the freshness window: anything older is
    already rejected as stale, so entries are evicted once they age out.
    """

    def __init__(self, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> None:
        self.max_age_seconds = max_age_seconds
        self._seen: dict[str, int] = {}

    def check(self, nonce_b64: str, ts: int, now: int | None = None) -> None:
        """Validate freshness and uniqueness, then record the nonce.

        Raises:
            StaleEnvelopeError: timestamp outside the freshness window.
            ReplayError: nonce already accepted.
        """
        now = int(time.time()) if now is None else now
        if abs(now - ts) > self.max_age_seconds:
            raise StaleEnvelopeError(f"Envelope timestamp {ts} outside ±{self.max_age_seconds}s window")

        if nonce_b64 in self._seen:
            raise ReplayError("Envelope nonce already used (replay)")

        cutoff = now - self.max_age_seconds
        if len(self._seen) > 512:
            self._seen = {n: t for n, t in self._seen.items() if t >= cutoff}
        self._seen[nonce_b64] = ts


def encrypt_payload(data: Any, key_bytes: bytes) -> dict[str, Any]:
    """Seal JSON-serializable data into an AES-256-GCM envelope."""
    plaintext = json.dumps(data).encode("utf-8")
    nonce = os.urandom(NONCE_BYTES)
    ts = int(time.time())
    ciphertext = AESGCM(key_bytes).encrypt(nonce, plaintext, _aad(ts))

    return {
        "encrypted": True,
        "v": E2EE_VERSION,
        "ts": ts,
        "nonce": base64.b64encode(nonce).decode("utf-8"),
        "data": base64.b64encode(ciphertext).decode("utf-8"),
    }


def decrypt_payload(
    envelope: dict[str, Any],
    key_bytes: bytes,
    guard: ReplayGuard | None = None,
    now: int | None = None,
) -> Any:
    """Open an AES-256-GCM envelope.

    Args:
        envelope: the sealed envelope.
        key_bytes: the 32-byte shared key.
        guard: optional replay cache. Callers handling untrusted input (the
            WebSocket receive path) should always pass one.
        now: override the current time, for tests.

    Raises:
        EnvelopeError: malformed, stale, or replayed envelope.
        InvalidTag: authentication failure (wrong key or tampering).
    """
    if not envelope.get("encrypted"):
        return envelope.get("data", envelope)

    if envelope.get("v") != E2EE_VERSION:
        raise EnvelopeError(f"Unsupported envelope version {envelope.get('v')!r}")

    ts = envelope.get("ts")
    nonce_b64 = envelope.get("nonce")
    data_b64 = envelope.get("data")
    if not isinstance(ts, int) or not isinstance(nonce_b64, str) or not isinstance(data_b64, str):
        raise EnvelopeError("Malformed envelope fields")

    if guard is not None:
        guard.check(nonce_b64, ts, now=now)

    nonce = base64.b64decode(nonce_b64)
    ciphertext = base64.b64decode(data_b64)
    plaintext = AESGCM(key_bytes).decrypt(nonce, ciphertext, _aad(ts))
    return json.loads(plaintext.decode("utf-8"))
