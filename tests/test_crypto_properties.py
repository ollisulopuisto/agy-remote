"""Small seeded crypto properties; exploratory runs can vary FUZZ_SEED locally."""

import base64
import os
import random
from unittest.mock import patch

import pytest
from cryptography.exceptions import InvalidTag

from agy_remote.crypto import ReplayError, ReplayGuard, StaleEnvelopeError, decrypt_payload, encrypt_payload

SEED = int(os.environ.get("FUZZ_SEED", "20261106"))
NOW = 1_800_000_000


@pytest.mark.parametrize("seed", [SEED, SEED + 1, SEED + 2])
def test_nested_payload_roundtrip_tamper_and_replay(seed):
    rng = random.Random(seed)
    for case in range(40):
        # Fixture-only bytes, not credentials. Keep generated payloads offline.
        key = rng.randbytes(32)
        payload = {
            "event": rng.choice(["step_added", "status", "question"]),
            "data": [
                None,
                bool(case % 2),
                rng.randrange(-10000, 10000),
                "".join(rng.choices("ä中😀\x00\n{}<>abc", k=rng.randrange(100))),
            ],
        }
        nonce = rng.randbytes(12)
        with (
            patch("agy_remote.crypto.time.time", return_value=NOW),
            patch("agy_remote.crypto.os.urandom", return_value=nonce),
        ):
            envelope = encrypt_payload(payload, key)
        context = f"seed={seed} case={case}"
        guard = ReplayGuard()
        assert decrypt_payload(envelope, key, guard, now=NOW) == payload, context
        with pytest.raises(ReplayError):
            decrypt_payload(envelope, key, guard, now=NOW)
        ciphertext = bytearray(base64.b64decode(envelope["data"]))
        ciphertext[rng.randrange(len(ciphertext))] ^= 1 << rng.randrange(8)
        altered = {**envelope, "data": base64.b64encode(ciphertext).decode()}
        with pytest.raises(InvalidTag):
            decrypt_payload(altered, key, ReplayGuard(), now=NOW)
        with pytest.raises(InvalidTag):
            decrypt_payload({**envelope, "ts": NOW + 1}, key, ReplayGuard(), now=NOW)
        with pytest.raises(StaleEnvelopeError):
            decrypt_payload(envelope, key, ReplayGuard(), now=NOW + 301)
