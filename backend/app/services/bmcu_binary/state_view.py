"""Shared read-model for the per-link BMCU status snapshot.

Both ``bmcu_monitors`` and ``bmcu_link`` expose the same realtime values; keeping
the projection here stops the two representations from drifting apart.
"""

from __future__ import annotations

from .bmcu_decoder import BMCUStatus

NO_SLOT = 0xFF


def motion_text(status: BMCUStatus) -> str:
    """Render per-slot motion as a stable comma separated string."""
    return ",".join(str(value) for value in status.motion)


def current_slot(status: BMCUStatus) -> int | None:
    return None if status.current_slot == NO_SLOT else status.current_slot


def pull_percent(status: BMCUStatus) -> int | None:
    slot = current_slot(status)
    if slot is None or slot >= len(status.pull_pct):
        return None
    return status.pull_pct[slot]


def status_snapshot(status: BMCUStatus, age_s: float | None = None) -> dict:
    """Flat dict of the realtime values, shared by both API surfaces."""
    return {
        "current_slot": current_slot(status),
        "inserted_mask": status.inserted_mask,
        "online_mask": status.online_mask,
        "motion": motion_text(status),
        "pull_pct": pull_percent(status),
        "pressure": status.pressure,
        "control_error": status.control_error,
        "age_s": age_s,
    }
