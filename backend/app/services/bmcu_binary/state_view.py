"""Shared read-model for the per-link BMCU status snapshot.

Both ``bmcu_monitors`` and ``bmcu_link`` expose the same realtime values; keeping
the projection here stops the two representations from drifting apart.
"""

from __future__ import annotations

from dataclasses import asdict

from .bmcu_decoder import BMCUStatus

NO_SLOT = 0xFF


def motion_values(status: BMCUStatus) -> list[int]:
    """Per-channel motion as numbers, in channel order.

    This used to be rendered as text -- first ``str(tuple)``, which put the
    Python repr ``"(0, 2, 0, 0)"`` on the wire, then a comma-joined string. Both
    made a consumer parse a number back out of prose, and the settings panel's
    per-channel motion row silently never rendered because it tested for an
    array. Four numbers stay four numbers.
    """
    return [int(value) for value in status.motion]


def current_slot(status: BMCUStatus) -> int | None:
    """Selected channel, or None for the 0xFF "nothing selected" sentinel."""
    return None if status.current_slot == NO_SLOT else status.current_slot


def pull_percent(status: BMCUStatus) -> int | None:
    """Pull reading of the selected channel; None when none is selected.

    None here means "no channel to report for", not "no data" -- the caller
    distinguishes the two by the link state, which is `no_data` in that case.
    """
    slot = current_slot(status)
    if slot is None or slot >= len(status.pull_pct):
        return None
    return status.pull_pct[slot]


def channel_flags(status: BMCUStatus) -> list[dict] | None:
    """Per-channel latches and switch reading, or None if unreported.

    Null rather than four zeroed channels: a frame that predates the flags byte
    says nothing about the latches, and rendering "no fault" for a channel that
    was never asked is the more expensive of the two mistakes.
    """
    if status.channel_flags is None:
        return None
    return [asdict(channel) for channel in status.channels]


def status_snapshot(status: BMCUStatus, age_s: float | None = None) -> dict:
    """Flat dict of the realtime values, shared by both API surfaces."""
    return {
        "current_slot": current_slot(status),
        "inserted_mask": status.inserted_mask,
        "online_mask": status.online_mask,
        "motion": motion_values(status),
        "pull_pct": pull_percent(status),
        "pressure": status.pressure,
        "control_error": status.control_error,
        "channel_flags": channel_flags(status),
        "age_s": age_s,
    }
