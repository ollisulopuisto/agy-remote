"""End-to-End Encryption (AES-256-GCM) for agy-remote."""

from __future__ import annotations

import base64
import json
import os
import secrets
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_e2ee_key() -> str:
    """Generate a 256-bit (32-byte) URL-safe base64 AES key."""
    raw_key = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw_key).decode("utf-8")


def decode_key(key_str: str) -> bytes:
    """Decode a base64 or hex key into raw bytes."""
    # Ensure correct padding for urlsafe base64
    padded = key_str + "=" * ((4 - len(key_str) % 4) % 4)
    return base64.urlsafe_b64decode(padded)


def encrypt_payload(data: Any, key_bytes: bytes) -> dict[str, Any]:
    """Encrypt JSON-serializable data using AES-256-GCM."""
    plaintext = json.dumps(data).encode("utf-8")
    aesgcm = AESGCM(key_bytes)
    nonce = os.urandom(12)  # 96-bit nonce
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)

    return {
        "encrypted": True,
        "nonce": base64.b64encode(nonce).decode("utf-8"),
        "data": base64.b64encode(ciphertext).decode("utf-8"),
    }


def decrypt_payload(envelope: dict[str, Any], key_bytes: bytes) -> Any:
    """Decrypt an AES-256-GCM encrypted envelope."""
    if not envelope.get("encrypted"):
        return envelope.get("data", envelope)

    nonce = base64.b64decode(envelope["nonce"])
    ciphertext = base64.b64decode(envelope["data"])
    aesgcm = AESGCM(key_bytes)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))
