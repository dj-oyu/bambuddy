"""Database-independent inputs for loader timeline and anomaly analysis."""

from __future__ import annotations

from dataclasses import dataclass

from .bmcu_decoder import BMCUEvent, BMCUFullStatusRecord, BMCUHello, BMCUStatus
from .constants import RecordType, StateField


@dataclass(frozen=True, slots=True)
class TimelinePoint:
    received_at_us: int
    link_index: int
    category: str
    slot: int | None
    value: int | tuple[int, ...]
    severity: int | None = None
    source: str = "bmcu"
    # STATE_CHANGE only: which field moved. Dropping it collapsed every state
    # change into one "state change" series, so a latching motion_fault was
    # indistinguishable from a slot selection (#5-adjacent, 2026-08-07).
    field: int | None = None


@dataclass(frozen=True, slots=True)
class AnomalyInput:
    received_at_us: int
    link_index: int
    kind: str
    severity: int
    slot: int | None
    value: int
    source: str = "bmcu"


def timeline_points(
    value: BMCUHello | BMCUStatus | BMCUEvent | BMCUFullStatusRecord,
    received_at_us: int,
    link_index: int,
) -> tuple[TimelinePoint, ...]:
    if isinstance(value, BMCUHello):
        return (
            TimelinePoint(
                received_at_us,
                link_index,
                "bmcu_boot",
                None,
                value.protocol_version,
            ),
        )
    if isinstance(value, BMCUStatus):
        return (
            (
                TimelinePoint(received_at_us, link_index, "current_slot", None, value.current_slot),
                TimelinePoint(received_at_us, link_index, "pressure", None, value.pressure),
            )
            + tuple(TimelinePoint(received_at_us, link_index, "motion", slot, value.motion[slot]) for slot in range(4))
            + tuple(
                TimelinePoint(received_at_us, link_index, "pull_pct", slot, value.pull_pct[slot]) for slot in range(4)
            )
        )
    if isinstance(value, BMCUFullStatusRecord):
        return (
            TimelinePoint(
                received_at_us,
                link_index,
                "full_status_record",
                None,
                value.record_type,
            ),
        )
    if value.record_type == RecordType.STATE_CHANGE and value.field is not None:
        return (
            TimelinePoint(
                received_at_us,
                link_index,
                "state_change",
                value.slot,
                value.value or 0,
                value.severity,
                "bmcu",
                value.field,
            ),
        )
    return (
        TimelinePoint(
            received_at_us,
            link_index,
            "event",
            value.slot,
            value.record_type,
            value.severity,
            "bmcu",
        ),
    )


def anomaly_inputs(
    value: BMCUHello | BMCUStatus | BMCUEvent | BMCUFullStatusRecord,
    received_at_us: int,
    link_index: int,
) -> tuple[AnomalyInput, ...]:
    if isinstance(value, BMCUEvent):
        # A latching motion fault is the one state change worth naming on its
        # own. Folded into `reported_event` it is invisible among slot and mask
        # changes, which is how a channel-1 fault sat latched from 2026-08-06
        # 21:31 through both feed pauses six hours later without ever being
        # reported. Emitted on the transition to set, not on the clear.
        if value.record_type == RecordType.STATE_CHANGE and value.field == StateField.MOTION_FAULT and value.value:
            return (
                AnomalyInput(
                    received_at_us,
                    link_index,
                    "motion_fault",
                    value.severity,
                    value.slot,
                    value.value,
                ),
            )
        if value.severity >= 3:
            return (
                AnomalyInput(
                    received_at_us,
                    link_index,
                    "reported_event",
                    value.severity,
                    value.slot,
                    value.record_type,
                ),
            )
    if isinstance(value, BMCUStatus) and value.control_error:
        return (
            AnomalyInput(
                received_at_us,
                link_index,
                "control_error",
                4,
                None,
                value.control_error,
            ),
        )
    return ()
