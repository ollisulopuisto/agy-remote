"""Unit tests for E2EE crypto module."""

from agy_remote.crypto import (
    decode_key,
    decrypt_payload,
    encrypt_payload,
    generate_e2ee_key,
)


def test_e2ee_key_generation():
    key = generate_e2ee_key()
    assert isinstance(key, str)
    raw = decode_key(key)
    assert len(raw) == 32


def test_e2ee_encrypt_decrypt_roundtrip():
    key_str = generate_e2ee_key()
    key_bytes = decode_key(key_str)

    sample_data = {
        "event": "step_added",
        "data": {
            "step": {"step_index": 1, "content": "Sensitive code / instructions"},
        },
    }

    envelope = encrypt_payload(sample_data, key_bytes)
    assert envelope["encrypted"] is True
    assert "nonce" in envelope
    assert "data" in envelope
    assert envelope["data"] != str(sample_data)

    decrypted = decrypt_payload(envelope, key_bytes)
    assert decrypted == sample_data
