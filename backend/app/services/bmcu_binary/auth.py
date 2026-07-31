"""BMB1 authentication transcript and HMAC helpers."""

from __future__ import annotations

import hashlib
import hmac

from .errors import AuthenticationError

AUTH_PREFIX = b"BMB1-AUTH"
SESSION_PREFIX = b"BMB1-SESSION"
CHALLENGE_SIZE = 32
DEVICE_KEY_SIZE = 32


def _check_sizes(device_key: bytes, challenge: bytes) -> None:
    if len(device_key) != DEVICE_KEY_SIZE:
        raise AuthenticationError("device key must be 32 bytes")
    if len(challenge) != CHALLENGE_SIZE:
        raise AuthenticationError("challenge must be 32 bytes")


def hello_mac(device_key: bytes, challenge: bytes, pico_boot_id: int, transcript: bytes) -> bytes:
    _check_sizes(device_key, challenge)
    data = AUTH_PREFIX + challenge + pico_boot_id.to_bytes(8, "big") + transcript
    return hmac.new(device_key, data, hashlib.sha256).digest()


def verify_hello_mac(
    device_key: bytes,
    challenge: bytes,
    pico_boot_id: int,
    transcript: bytes,
    supplied: bytes,
) -> bool:
    if len(supplied) != hashlib.sha256().digest_size:
        return False
    return hmac.compare_digest(hello_mac(device_key, challenge, pico_boot_id, transcript), supplied)


def derive_session_key(device_key: bytes, challenge: bytes, pico_boot_id: int) -> bytes:
    _check_sizes(device_key, challenge)
    data = SESSION_PREFIX + challenge + pico_boot_id.to_bytes(8, "big")
    return hmac.new(device_key, data, hashlib.sha256).digest()


def control_mac(session_key: bytes, transport_header: bytes, payload_without_mac: bytes) -> bytes:
    return hmac.new(session_key, transport_header + payload_without_mac, hashlib.sha256).digest()
