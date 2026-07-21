"""Filament feed-stall detection from BMCU Link telemetry (private fork).

Incident (2026-07-21): the BMCU silently stopped driving slot 4 mid-print —
channel telemetry showed pull_pct pinned at 0 with the motor off and
controller_motion=6 (stop-on-use) while ams_motion stayed 2 (on-use). No HMS
error, no motion_fault: the printer air-printed for hours and reported a
normal FINISH at full layer count, twice in a row. The firmware will not tell
us; the only observable is the pull buffer collapsing and staying collapsed
while the extruder keeps consuming G-code.

Detection is deliberately a compound condition — pull_pct alone is NOT an
anomaly (it legitimately sits at extremes during unload/idle):

    printer RUNNING
    AND BMCU status fresh (stale link = no data, never evidence)
    AND ams motion of the active slot is on-use
    AND pull_pct[active slot] <= starve threshold
    AND all of the above continuously for `after_s`

Two stages: an early WARNING (notify-only) fires after `warn_after_s` once
pull_pct leaves the neutral band — a human head start while the buffer is
still draining — and the PAUSE trigger fires after `after_s` of confirmed
starvation. Both latch per episode; recovery above the neutral band re-arms.

Recovery semantics matter for the print: Bambu MQTT cannot rewind G-code, so
layers consumed while starving become a permanent void in the part. The whole
point of this watcher is to keep that void to 1-2 layers by pausing fast; the
episode also records the layer where degradation began (pull_pct first left
the neutral band) so the notification can report the estimated damage range
and the user can decide resume-vs-restart.

Pure logic lives in FeedStallDetector (unit-testable, no I/O); the asyncio
loop, pause command and notification wiring live in main.py next to the
stall-notify watcher it mirrors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ams_motion value meaning "on-use" (PICO_BAMBUDDY_OUTPUT.md §2.2 — the slot
# currently feeding the print).
_AMS_MOTION_ON_USE = 2


@dataclass
class FeedStallEpisode:
    starved_since: float
    # Layer where pull_pct first left the neutral band this episode — lower
    # bound of the damage estimate. Falls back to the trigger layer when the
    # decline was never observed (e.g. already starved at watcher start).
    degraded_layer: int | None
    slot: int
    notified: bool = False
    pull_history: list[int] = field(default_factory=list)


@dataclass
class FeedWarnEpisode:
    degraded_since: float
    degraded_layer: int
    slot: int
    notified: bool = False


@dataclass(frozen=True)
class FeedStallWarning:
    """Early sign, notify-only: pull_pct left the neutral band while on-use.
    Fires once per episode, well before the pause trigger — gives a human a
    head start while the buffer is still draining."""

    slot: int
    degraded_for_s: float
    degraded_layer: int
    current_layer: int
    pull_pct: int


@dataclass(frozen=True)
class FeedStallTrigger:
    slot: int
    starved_for_s: float
    degraded_layer: int | None
    current_layer: int
    pull_pct: int


class FeedStallDetector:
    """Per-printer episode tracker. observe() is called on every tick with the
    printer's and the BMCU's current state; returns a FeedStallTrigger exactly
    when the alarm should fire (caller latches via mark_notified so a failed
    send retries next tick — same contract as the stall-notify watcher)."""

    def __init__(
        self,
        *,
        starve_pct: int = 5,
        neutral_pct: int = 20,
        after_s: float = 30.0,
        warn_after_s: float = 15.0,
        max_age_s: float = 20.0,
    ) -> None:
        self.starve_pct = starve_pct
        self.neutral_pct = neutral_pct
        self.after_s = after_s
        self.warn_after_s = warn_after_s
        self.max_age_s = max_age_s
        self._episodes: dict[int, FeedStallEpisode] = {}
        self._warn_episodes: dict[int, FeedWarnEpisode] = {}
        # printer_id -> layer at which pull_pct last dipped below neutral_pct
        # while healthy — pre-episode memory for the damage lower bound.
        self._degraded_layer: dict[int, int | None] = {}

    def observe(
        self,
        printer_id: int,
        *,
        printing: bool,
        layer_num: int,
        status: dict | None,
        age_s: float,
        now: float,
    ) -> FeedStallTrigger | FeedStallWarning | None:
        """status is the inner BMCU status dict (pull_pct/motion/current_slot).
        Any missing/degenerate input dissolves the episode — FAIL-SAFE toward
        "no alarm, no pause": a spurious pause scars a healthy print."""
        if not printing:
            self._reset(printer_id)
            return None
        if status is None or age_s > self.max_age_s:
            # Stale link: freeze rather than reset would risk pausing off
            # minutes-old data after a resync flood (observed 2026-07-21, 39万
            # dropped envelopes). No data = no episode.
            self._reset(printer_id)
            return None

        slot = status.get("current_slot")
        pull = status.get("pull_pct")
        motion = status.get("motion")
        if (
            not isinstance(slot, int)
            or not isinstance(pull, list)
            or not isinstance(motion, list)
            or not (0 <= slot < len(pull))
            or slot >= len(motion)
        ):
            self._reset(printer_id)
            return None
        try:
            slot_pull = int(pull[slot])
            slot_motion = int(motion[slot])
        except (TypeError, ValueError):
            self._reset(printer_id)
            return None

        on_use = slot_motion == _AMS_MOTION_ON_USE
        degraded = on_use and slot_pull < self.neutral_pct

        # Damage lower bound: remember the layer where pull first left the
        # neutral band (tracked even before the starve threshold is crossed).
        if not degraded:
            self._degraded_layer[printer_id] = None
        elif self._degraded_layer.get(printer_id) is None:
            self._degraded_layer[printer_id] = layer_num

        # Early-warning stage: notify-only, short confirm window. A recovery
        # back above the neutral band dissolves the episode, so a re-collapse
        # later in the print warns again.
        warning: FeedStallWarning | None = None
        wep = self._warn_episodes.get(printer_id)
        if not degraded:
            if wep is not None:
                del self._warn_episodes[printer_id]
        else:
            if wep is None or wep.slot != slot:
                wep = FeedWarnEpisode(
                    degraded_since=now,
                    degraded_layer=self._degraded_layer.get(printer_id) or layer_num,
                    slot=slot,
                )
                self._warn_episodes[printer_id] = wep
            if not wep.notified and (now - wep.degraded_since) >= self.warn_after_s:
                warning = FeedStallWarning(
                    slot=slot,
                    degraded_for_s=now - wep.degraded_since,
                    degraded_layer=wep.degraded_layer,
                    current_layer=layer_num,
                    pull_pct=slot_pull,
                )

        starving = on_use and slot_pull <= self.starve_pct
        ep = self._episodes.get(printer_id)
        if not starving:
            if ep is not None:
                del self._episodes[printer_id]
            return warning

        if ep is None or ep.slot != slot:
            ep = FeedStallEpisode(
                starved_since=now,
                degraded_layer=self._degraded_layer.get(printer_id, layer_num),
                slot=slot,
            )
            self._episodes[printer_id] = ep
        ep.pull_history.append(slot_pull)
        del ep.pull_history[:-30]

        starved_for = now - ep.starved_since
        if ep.notified:
            # Pause stage already handled this episode — a late warning for
            # the same collapse is noise.
            return None
        if starved_for < self.after_s:
            return warning
        # The pause trigger supersedes a same-tick warning: one notification,
        # the serious one.
        return FeedStallTrigger(
            slot=slot,
            starved_for_s=starved_for,
            degraded_layer=ep.degraded_layer,
            current_layer=layer_num,
            pull_pct=slot_pull,
        )

    def mark_notified(self, printer_id: int) -> None:
        ep = self._episodes.get(printer_id)
        if ep is not None:
            ep.notified = True

    def mark_warned(self, printer_id: int) -> None:
        wep = self._warn_episodes.get(printer_id)
        if wep is not None:
            wep.notified = True

    def _reset(self, printer_id: int) -> None:
        self._episodes.pop(printer_id, None)
        self._warn_episodes.pop(printer_id, None)
        self._degraded_layer.pop(printer_id, None)
