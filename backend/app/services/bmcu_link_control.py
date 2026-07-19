"""BMCU Link CONTROL message authentication (firmware issue #2).

Implements the bambuddy (originator) side of the CONTROL security contract
agreed on dj-oyu/BMCU-C-PJARCZAK-kaizou issue #2:

- A per-device ``control_key`` provisioned separately from the telemetry
  token, stored Fernet-encrypted at rest, never sent on the wire.
- HMAC-SHA256 over a fixed-order, length-prefixed byte encoding of the
  CONTROL message fields (no dependence on JSON canonicalization).
- A monotonically increasing ``control_sequence`` per (device,
  pico_boot_session, session_nonce), persisted in the device row so a
  bambuddy restart cannot reuse a sequence number within a live session.
- Command results are MAC'd with the same key over a distinct context
  string, so a result can never be replayed as a command (and vice versa).

MAC input byte contract v1 (proposed to firmware on issue #2; both sides
must produce identical bytes):

    mac_input = CTX || u8(version) || LP(device_id) || LP(link_id)
                || LP(pico_boot_session) || LP(session_nonce)
                || u64be(control_sequence) || LP(operation_id)
                || LP(command) || u32be(ttl_ms) || LP(payload_b64)

    LP(s)  = u16be(len(utf8(s))) || utf8(s)
    CTX    = b"BMCU-CTRL-v1"        for CONTROL messages
             b"BMCU-CTRL-RES-v1"    for command results (see below)

Result MAC input v1:

    res_input = b"BMCU-CTRL-RES-v1" || u8(version) || LP(device_id)
                || LP(link_id) || LP(pico_boot_session) || LP(session_nonce)
                || u64be(control_sequence) || LP(operation_id)
                || LP(status) || LP(result_payload_b64)

``mac`` is lowercase hex of the HMAC-SHA256 digest.

CONTROL itself is v1 out-of-scope on the wire (firmware Phase 5 is
telemetry-only); this module exists so the contract is pinned by tests on
both sides before any command path is enabled. Fail-safe: no key
provisioned -> no message can be built; verification failures raise.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass

MAC_VERSION = 1

_CTX_CONTROL = b"BMCU-CTRL-v1"
_CTX_RESULT = b"BMCU-CTRL-RES-v1"

# Message-level TTL bounds (ms). Command payloads may impose tighter caps
# (e.g. soft reset's payload ttl_ms is 1..5000 per issue #3).
TTL_MS_MIN = 1
TTL_MS_MAX = 60_000

# Field length ceiling for LP() — u16 length prefix, but nothing legitimate
# approaches it; reject early instead of truncating.
_MAX_FIELD_LEN = 1024


class ControlContractError(ValueError):
    """A field violates the CONTROL byte contract (fail-closed)."""


def generate_control_key() -> str:
    """Mint a new device control key: 32 random bytes, lowercase hex.

    Hex (not urlsafe) so the Pico commissioning UI can accept it with a
    trivial parser and both sides derive identical key bytes via unhexlify.
    """
    return secrets.token_hex(32)


def _key_bytes(control_key: str) -> bytes:
    try:
        raw = bytes.fromhex(control_key.strip())
    except ValueError as e:
        raise ControlContractError(f"control_key is not hex: {e}") from e
    if len(raw) != 32:
        raise ControlContractError("control_key must be 32 bytes (64 hex chars)")
    return raw


def _lp(field_name: str, value: str) -> bytes:
    if not isinstance(value, str):
        raise ControlContractError(f"{field_name} must be a string")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_FIELD_LEN:
        raise ControlContractError(f"{field_name} exceeds {_MAX_FIELD_LEN} bytes")
    return struct.pack(">H", len(encoded)) + encoded


@dataclass(frozen=True)
class ControlMessage:
    """The MAC-covered fields of an outbound CONTROL message."""

    device_id: str
    link_id: str
    pico_boot_session: str
    session_nonce: str
    control_sequence: int
    operation_id: str
    command: str
    ttl_ms: int
    payload_b64: str
    version: int = MAC_VERSION


def build_mac_input(msg: ControlMessage) -> bytes:
    """Serialize the CONTROL message fields into the v1 MAC input bytes."""
    if msg.version != MAC_VERSION:
        raise ControlContractError(f"unsupported MAC version: {msg.version}")
    if not (0 <= msg.control_sequence < 2**64):
        raise ControlContractError("control_sequence out of u64 range")
    if not (TTL_MS_MIN <= msg.ttl_ms <= TTL_MS_MAX):
        raise ControlContractError(f"ttl_ms out of range [{TTL_MS_MIN}, {TTL_MS_MAX}]")
    return (
        _CTX_CONTROL
        + struct.pack(">B", msg.version)
        + _lp("device_id", msg.device_id)
        + _lp("link_id", msg.link_id)
        + _lp("pico_boot_session", msg.pico_boot_session)
        + _lp("session_nonce", msg.session_nonce)
        + struct.pack(">Q", msg.control_sequence)
        + _lp("operation_id", msg.operation_id)
        + _lp("command", msg.command)
        + struct.pack(">I", msg.ttl_ms)
        + _lp("payload_b64", msg.payload_b64)
    )


def sign_control_message(control_key: str, msg: ControlMessage) -> str:
    """HMAC-SHA256 the message; returns lowercase hex ``mac``."""
    return hmac.new(_key_bytes(control_key), build_mac_input(msg), hashlib.sha256).hexdigest()


def build_control_frame(control_key: str, msg: ControlMessage) -> dict:
    """Assemble the full wire-format CONTROL JSON object (issue #2 §3)."""
    return {
        "type": "control",
        "version": msg.version,
        "device_id": msg.device_id,
        "link_id": msg.link_id,
        "pico_boot_session": msg.pico_boot_session,
        "session_nonce": msg.session_nonce,
        "control_sequence": msg.control_sequence,
        "operation_id": msg.operation_id,
        "command": msg.command,
        "ttl_ms": msg.ttl_ms,
        "payload_b64": msg.payload_b64,
        "mac": sign_control_message(control_key, msg),
    }


def build_result_mac_input(
    *,
    device_id: str,
    link_id: str,
    pico_boot_session: str,
    session_nonce: str,
    control_sequence: int,
    operation_id: str,
    status: str,
    result_payload_b64: str,
    version: int = MAC_VERSION,
) -> bytes:
    """Serialize command-result fields into the v1 result MAC input bytes.

    A distinct context string keeps result MACs from ever validating as
    command MACs even though they share the key and session identity.
    """
    if version != MAC_VERSION:
        raise ControlContractError(f"unsupported MAC version: {version}")
    if not (0 <= control_sequence < 2**64):
        raise ControlContractError("control_sequence out of u64 range")
    return (
        _CTX_RESULT
        + struct.pack(">B", version)
        + _lp("device_id", device_id)
        + _lp("link_id", link_id)
        + _lp("pico_boot_session", pico_boot_session)
        + _lp("session_nonce", session_nonce)
        + struct.pack(">Q", control_sequence)
        + _lp("operation_id", operation_id)
        + _lp("status", status)
        + _lp("result_payload_b64", result_payload_b64)
    )


def verify_result_mac(control_key: str, result: dict) -> bool:
    """Constant-time verification of a command result's ``mac`` field.

    Returns False on any missing/ill-typed field rather than raising —
    callers treat an unverifiable result exactly like a forged one.

    Contract note: an omitted ``result_payload_b64`` is MAC'd as the empty
    string — the firmware side must cover the identical default.

    This proves authenticity only. When the CONTROL send path is wired up,
    the caller MUST additionally match (operation_id, control_sequence)
    against its own outstanding command — a captured valid result is
    otherwise replayable.
    """
    try:
        expected = hmac.new(
            _key_bytes(control_key),
            build_result_mac_input(
                device_id=result["device_id"],
                link_id=result["link_id"],
                pico_boot_session=result["pico_boot_session"],
                session_nonce=result["session_nonce"],
                control_sequence=int(result["control_sequence"]),
                operation_id=result["operation_id"],
                status=result["status"],
                result_payload_b64=result.get("result_payload_b64", ""),
                version=int(result.get("version", MAC_VERSION)),
            ),
            hashlib.sha256,
        ).hexdigest()
        provided = result["mac"]
        if not isinstance(provided, str):
            return False
        return hmac.compare_digest(expected, provided.lower())
    except (KeyError, TypeError, ValueError, ControlContractError):
        return False


# --- DB-backed key storage and sequence allocation -------------------------
#
# Imported lazily where used so the pure-crypto part of this module stays
# importable without an app database (tests, tooling, potential reuse in the
# firmware repo's host-side test harness).


async def set_control_key(db, device) -> str:
    """Generate and store a new control key for ``device`` (BMCULinkDevice).

    Returns the plaintext key exactly once — the caller shows it to the
    admin for entry into the Pico commissioning UI and must not log it.
    Rotating (calling again) invalidates the old key immediately.
    """
    from datetime import datetime, timezone

    from backend.app.core.encryption import mfa_encrypt

    key = generate_control_key()
    encrypted = mfa_encrypt(key)
    # mfa_encrypt silently degrades to plaintext when no Fernet key is
    # configured — acceptable for TOTP secrets (legacy), NOT for a
    # long-lived command-authentication key. Fail closed instead.
    if not encrypted.startswith("fernet:"):
        raise ControlContractError(
            "at-rest encryption unavailable (no MFA_ENCRYPTION_KEY / data-dir key); refusing to store control key"
        )
    device.control_key_encrypted = encrypted
    device.control_key_set_at = datetime.now(timezone.utc)
    # New key => any in-flight session state is void; restart sequencing.
    device.control_session_nonce = None
    device.control_sequence = 0
    await db.commit()
    return key


async def revoke_control_key(db, device) -> None:
    """Remove the device's control key. CONTROL becomes impossible (fail-safe)."""
    device.control_key_encrypted = None
    device.control_key_set_at = None
    device.control_session_nonce = None
    device.control_sequence = 0
    await db.commit()


def get_control_key(device) -> str | None:
    """Decrypt the stored control key, or None if not provisioned."""
    if not device.control_key_encrypted:
        return None
    from backend.app.core.encryption import mfa_decrypt

    return mfa_decrypt(device.control_key_encrypted)


async def allocate_control_sequence(db, device, session_nonce: str) -> int:
    """Return the next monotonic control_sequence for the given session.

    Persisted before use: the row is committed with the allocated value, so
    a crash between allocation and send burns the number instead of ever
    reusing it. A new ``session_nonce`` (new Pico boot/session) resets the
    counter to 0, matching the Pico's per-session replay window.
    """
    if not session_nonce:
        raise ControlContractError("session_nonce is required")
    from sqlalchemy import update

    from backend.app.models.bmcu_link_device import BMCULinkDevice

    # Atomic in SQL — a Python read-modify-write could hand the same number
    # to two concurrent callers. The nonce guard on the increment means a
    # concurrent session change yields no allocation instead of a wrong one.
    await db.execute(
        update(BMCULinkDevice)
        .where(
            BMCULinkDevice.id == device.id,
            (BMCULinkDevice.control_session_nonce.is_(None))
            | (BMCULinkDevice.control_session_nonce != session_nonce),
        )
        .values(control_session_nonce=session_nonce, control_sequence=0)
    )
    result = await db.execute(
        update(BMCULinkDevice)
        .where(
            BMCULinkDevice.id == device.id,
            BMCULinkDevice.control_session_nonce == session_nonce,
        )
        .values(control_sequence=BMCULinkDevice.control_sequence + 1)
        .returning(BMCULinkDevice.control_sequence)
    )
    row = result.scalar_one_or_none()
    await db.commit()
    if row is None:
        raise ControlContractError("session changed during sequence allocation")
    await db.refresh(device)
    return row - 1
