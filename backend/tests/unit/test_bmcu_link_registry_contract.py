"""Pin the decoder's hand-written kind numbers to the mirrored link registry.

``docs/bmcu_link_enum_registry.json`` is one of the two companions
BMCU_BINARY_TRANSPORT_V1.md's Normative references section requires this
repository to carry. Carrying a file nothing reads is how the last copy drifted,
so these tests make it load-bearing: every bare integer the decoder and the
timeline projection spell out is checked against the name the registry gives it.

The transport registry (``docs/bmcu_binary_registry.json``, pinned separately by
test_bmcu_binary_codec) deliberately carries none of this. That split being
invisible is what cost bambuddy the decode in the first place -- kinds 16, 18,
23, 114 and 127 were stored as unknown when they are `get_status`, `ping`,
`get_full_status`, `pong` and `ack`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.services.bmcu_binary.bmcu_decoder import (
    FULL_STATUS_COUNTERS,
    FULL_STATUS_GLOBAL,
    SEMANTIC_KINDS,
)

REGISTRY = json.loads((Path(__file__).parents[3] / "docs" / "bmcu_link_enum_registry.json").read_text())
ENUMS = REGISTRY["enums"]


def _value(enum: str, name: str) -> int:
    """The single wire value the registry gives that name."""
    matches = [int(key) for key, value in ENUMS[enum].items() if value == name]
    assert len(matches) == 1, f"{name} is not a unique {enum}: {matches}"
    return matches[0]


@pytest.mark.parametrize("name", ["hello", "status", "event", "full_status_record"])
def test_every_semantic_kind_is_one_the_registry_names(name: str) -> None:
    assert _value("kind", name) in SEMANTIC_KINDS


def test_semantic_kinds_holds_nothing_the_decoder_cannot_turn_into_a_value() -> None:
    """The set doubles as a query filter, so a stray kind spends a row budget.

    Everything else on the wire -- the request kinds and the keepalives -- is
    stored but carries no loader state, and the timeline excludes it by this set.
    """
    named = {_value("kind", name) for name in ("hello", "status", "event", "full_status_record")}
    assert named == SEMANTIC_KINDS
    keepalives = {_value("kind", name) for name in ("ping", "pong", "ack", "get_status", "get_full_status")}
    assert not SEMANTIC_KINDS & keepalives


def test_full_status_record_types_match_the_registry() -> None:
    assert _value("full_status_record_type", "global") == FULL_STATUS_GLOBAL
    assert _value("full_status_record_type", "counters") == FULL_STATUS_COUNTERS


def test_state_change_is_the_event_record_type_the_decoder_unpacks() -> None:
    """decode_semantic reads field/slot/previous/value only for this one."""
    from backend.app.services.bmcu_binary.bmcu_decoder import decode_semantic, decode_wire_frame
    from backend.tests.unit.test_bmcu_monitor_schema_contract import _wire

    state_change = _value("record_type", "state_change")
    payload = bytearray(16)
    payload[4], payload[5], payload[6], payload[7] = state_change, 2, 1, 6
    payload[8:14] = bytes((9, 8, 7, 0, 6, 0))
    event = decode_semantic(decode_wire_frame(_wire(3, bytes(payload))))
    assert (event.field, event.slot) == (9, 8)

    # Any other record type keeps the union opaque rather than guessing at it.
    payload[4] = _value("record_type", "boot")
    other = decode_semantic(decode_wire_frame(_wire(3, bytes(payload))))
    assert other.field is None and other.slot is None


def test_timeline_severity_buckets_start_at_the_registry_warning_level() -> None:
    """`anomaly` is warning-and-above, so the boundary must be the named one."""
    from backend.app.services.bmcu_binary.bmcu_decoder import BMCUEvent
    from backend.app.services.bmcu_binary.timeline import anomaly_inputs

    warning = _value("severity", "warning")
    assert anomaly_inputs(BMCUEvent(0, 1, warning, 1, b""), 0, 0)
    assert not anomaly_inputs(BMCUEvent(0, 1, warning - 1, 1, b""), 0, 0)


def test_every_state_field_the_registry_names_has_a_constant() -> None:
    """The names are load-bearing now: the timeline labels state changes by
    them, so a field the registry gained and the enum did not would reach the
    client unnamed."""
    from backend.app.services.bmcu_binary.constants import StateField

    for key, name in ENUMS["state_field"].items():
        assert StateField(int(key)).name.lower() == name


def test_state_field_names_round_trip_and_unknown_stays_unknown() -> None:
    from backend.app.services.bmcu_binary.constants import state_field_name

    assert state_field_name(_value("state_field", "motion_fault")) == "motion_fault"
    assert state_field_name(_value("state_field", "control_error")) == "control_error"
    # "a field this build has no name for" must not be confused with "not a
    # state change" — both are None to the caller, but only one is a gap.
    assert state_field_name(max(int(k) for k in ENUMS["state_field"]) + 1) is None
    assert state_field_name(None) is None


def test_record_types_match_the_registry() -> None:
    from backend.app.services.bmcu_binary.constants import RecordType

    for key, name in ENUMS["record_type"].items():
        assert RecordType(int(key)).name.lower() == name


def test_registry_is_the_link_one_and_not_the_transport_one() -> None:
    """The two registries do not overlap; mirroring only one caused this issue."""
    transport = json.loads((Path(__file__).parents[3] / "docs" / "bmcu_binary_registry.json").read_text())
    assert "kind" in ENUMS and "message_types" not in ENUMS
    assert "message_types" in transport and "kind" not in transport
