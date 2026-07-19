"""BMCU Link CONTROL security contract tests (firmware issue #2).

These pin the v1 MAC byte contract so both repos can implement identical
bytes: any change to the encoding breaks these tests loudly.
"""

import hashlib
import hmac
import struct

import pytest

from backend.app.models.bmcu_link_device import BMCULinkDevice
from backend.app.services.bmcu_link_control import (
    ControlContractError,
    ControlMessage,
    allocate_control_sequence,
    build_control_frame,
    build_mac_input,
    build_result_mac_input,
    generate_control_key,
    get_control_key,
    revoke_control_key,
    set_control_key,
    sign_control_message,
    verify_result_mac,
)

KEY = "aa" * 32  # fixed test key: 32 bytes of 0xAA


def make_msg(**overrides) -> ControlMessage:
    fields = dict(
        device_id="pico-1",
        link_id="default",
        pico_boot_session="boot-abc",
        session_nonce="nonce-1",
        control_sequence=17,
        operation_id="op-42",
        command="soft_reset",
        ttl_ms=5000,
        payload_b64="AQID",
    )
    fields.update(overrides)
    return ControlMessage(**fields)


def lp(s: str) -> bytes:
    e = s.encode("utf-8")
    return struct.pack(">H", len(e)) + e


class TestMacInput:
    def test_exact_bytes(self):
        """Known-answer test for the v1 byte layout — the cross-repo contract."""
        expected = (
            b"BMCU-CTRL-v1"
            + struct.pack(">B", 1)
            + lp("pico-1")
            + lp("default")
            + lp("boot-abc")
            + lp("nonce-1")
            + struct.pack(">Q", 17)
            + lp("op-42")
            + lp("soft_reset")
            + struct.pack(">I", 5000)
            + lp("AQID")
        )
        assert build_mac_input(make_msg()) == expected

    def test_mac_matches_independent_hmac(self):
        mac = sign_control_message(KEY, make_msg())
        ref = hmac.new(bytes.fromhex(KEY), build_mac_input(make_msg()), hashlib.sha256).hexdigest()
        assert mac == ref

    @pytest.mark.parametrize(
        "field,value",
        [
            ("device_id", "pico-2"),
            ("link_id", "bmcu-a"),
            ("pico_boot_session", "boot-xyz"),
            ("session_nonce", "nonce-2"),
            ("control_sequence", 18),
            ("operation_id", "op-43"),
            ("command", "led_set"),
            ("ttl_ms", 4999),
            ("payload_b64", "AQIE"),
        ],
    )
    def test_any_field_change_changes_mac(self, field, value):
        assert sign_control_message(KEY, make_msg()) != sign_control_message(KEY, make_msg(**{field: value}))

    def test_no_field_boundary_ambiguity(self):
        """Length-prefixing means shifting a byte across a field boundary
        cannot produce the same MAC input (the classic concat pitfall)."""
        a = make_msg(device_id="ab", link_id="c")
        b = make_msg(device_id="a", link_id="bc")
        assert build_mac_input(a) != build_mac_input(b)

    def test_ttl_bounds(self):
        with pytest.raises(ControlContractError):
            build_mac_input(make_msg(ttl_ms=0))
        with pytest.raises(ControlContractError):
            build_mac_input(make_msg(ttl_ms=60_001))

    def test_sequence_u64_bounds(self):
        with pytest.raises(ControlContractError):
            build_mac_input(make_msg(control_sequence=-1))
        with pytest.raises(ControlContractError):
            build_mac_input(make_msg(control_sequence=2**64))
        build_mac_input(make_msg(control_sequence=2**64 - 1))  # max ok

    def test_oversized_field_rejected(self):
        with pytest.raises(ControlContractError):
            build_mac_input(make_msg(payload_b64="A" * 2000))

    def test_bad_key_rejected(self):
        with pytest.raises(ControlContractError):
            sign_control_message("nothex", make_msg())
        with pytest.raises(ControlContractError):
            sign_control_message("aa" * 16, make_msg())  # 16 bytes, need 32

    def test_frame_shape(self):
        frame = build_control_frame(KEY, make_msg())
        assert frame["type"] == "control"
        assert frame["mac"] == sign_control_message(KEY, make_msg())
        assert frame["control_sequence"] == 17
        assert "control_key" not in frame


def make_result(key=KEY, **overrides) -> dict:
    fields = dict(
        version=1,
        device_id="pico-1",
        link_id="default",
        pico_boot_session="boot-abc",
        session_nonce="nonce-1",
        control_sequence=17,
        operation_id="op-42",
        status="ack_ok",
        result_payload_b64="",
    )
    fields.update(overrides)
    mac = hmac.new(
        bytes.fromhex(key),
        build_result_mac_input(
            device_id=fields["device_id"],
            link_id=fields["link_id"],
            pico_boot_session=fields["pico_boot_session"],
            session_nonce=fields["session_nonce"],
            control_sequence=fields["control_sequence"],
            operation_id=fields["operation_id"],
            status=fields["status"],
            result_payload_b64=fields["result_payload_b64"],
            version=fields["version"],
        ),
        hashlib.sha256,
    ).hexdigest()
    return {**fields, "mac": mac}


class TestResultMac:
    def test_roundtrip(self):
        assert verify_result_mac(KEY, make_result()) is True

    def test_uppercase_mac_accepted(self):
        r = make_result()
        r["mac"] = r["mac"].upper()
        assert verify_result_mac(KEY, r) is True

    def test_tampered_status_rejected(self):
        r = make_result()
        r["status"] = "ack_denied"
        assert verify_result_mac(KEY, r) is False

    def test_wrong_key_rejected(self):
        assert verify_result_mac("bb" * 32, make_result()) is False

    def test_missing_field_is_unverifiable_not_crash(self):
        r = make_result()
        del r["operation_id"]
        assert verify_result_mac(KEY, r) is False

    def test_context_separation(self):
        """A CONTROL message MAC must never validate as a result MAC even
        with identical field values — distinct context strings guarantee it."""
        msg = make_msg()
        control_mac = sign_control_message(KEY, msg)
        r = make_result(status=msg.command)
        r["mac"] = control_mac
        assert verify_result_mac(KEY, r) is False


class TestKeyStorageAndSequence:
    @pytest.mark.asyncio
    async def test_key_provision_rotate_revoke(self, db_session):
        device = BMCULinkDevice(device_id="pico-1")
        db_session.add(device)
        await db_session.commit()

        key1 = await set_control_key(db_session, device)
        assert len(key1) == 64 and bytes.fromhex(key1)
        assert device.control_key_encrypted is not None
        assert key1 not in device.control_key_encrypted  # encrypted at rest
        assert get_control_key(device) == key1

        key2 = await set_control_key(db_session, device)  # rotate
        assert key2 != key1
        assert get_control_key(device) == key2

        await revoke_control_key(db_session, device)
        assert get_control_key(device) is None
        assert device.control_sequence == 0

    @pytest.mark.asyncio
    async def test_sequence_monotonic_and_session_reset(self, db_session):
        device = BMCULinkDevice(device_id="pico-1")
        db_session.add(device)
        await db_session.commit()

        assert await allocate_control_sequence(db_session, device, "nonce-1") == 0
        assert await allocate_control_sequence(db_session, device, "nonce-1") == 1
        assert await allocate_control_sequence(db_session, device, "nonce-1") == 2
        # New Pico session -> counter resets (Pico's replay window is per session)
        assert await allocate_control_sequence(db_session, device, "nonce-2") == 0
        assert await allocate_control_sequence(db_session, device, "nonce-2") == 1

    @pytest.mark.asyncio
    async def test_sequence_requires_nonce(self, db_session):
        device = BMCULinkDevice(device_id="pico-1")
        db_session.add(device)
        await db_session.commit()
        with pytest.raises(ControlContractError):
            await allocate_control_sequence(db_session, device, "")

    def test_generated_keys_unique(self):
        assert generate_control_key() != generate_control_key()

    @pytest.mark.asyncio
    async def test_provision_fails_closed_without_encryption(self, db_session, monkeypatch):
        """mfa_encrypt degrades to plaintext without a Fernet key; the
        control key must refuse to be stored rather than land unencrypted."""
        import backend.app.core.encryption as enc

        monkeypatch.setattr(enc, "mfa_encrypt", lambda s: s)
        device = BMCULinkDevice(device_id="pico-x")
        db_session.add(device)
        await db_session.commit()
        with pytest.raises(ControlContractError):
            await set_control_key(db_session, device)
        assert device.control_key_encrypted is None
