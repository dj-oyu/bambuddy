"""Auto-press "Resume" on the HMS faults that always clear by resuming.

Why this exists (2026-08-07, live): item 224 started, swapped from tray 3 to
tray 1, and the printer paused at layer 0 with

    0700_8006  sev 3  actions ["CONTINUE", "CHECK_ASSISTANT"]
               "Unable to feed filament into the extruder..."
    0700_0006  sev 1  (no description in the HMS catalog)

On this rig that fault resolves every time by pressing Resume, with nothing
physically wrong and nothing for the operator to do. Left alone it holds the
printer indefinitely — a five-minute stall notification and then silence until
somebody wakes up. Unattended overnight printing is the whole point of the
queue, so a fault whose only remedy is "press the button" should not need a
human awake to press it.

This is deliberately NOT an extension of the 0500_409D auto-clear path in
``hms_retry``. That machine clears the HMS list and *requeues the queue item*
when the resume does not take, because 409D rejects a print at start while the
printer stays IDLE — nothing is in flight to lose. Here the print IS in
flight: a requeue would restart a job that is already several hours in. So
this module never touches the queue and never clears the HMS list; the only
command it can issue is the CONTINUE action the firmware itself offered on the
error, which is exactly what the UI's Resume button sends
(``execute_hms_action`` -> ``ams_control("resume")``).

Design (user-approved 2026-08-07):

- **Allowlist, never "anything with a CONTINUE button".** Blind-resuming an
  unknown fault is how a nozzle jam becomes a printed-in-air failure. Only
  codes named in ``BAMBUDDY_HMS_AUTO_CONTINUE_CODES`` fire, and only when the
  firmware actually offered CONTINUE on that error this time.
- **Bounded per print job.** Three resumes per ``job_id``, then stop and tell a
  human. A fault that survives three resumes is not the benign one this module
  was written for. The counter follows the job, not the pause, so a print that
  pauses again after resuming keeps counting toward the same budget.
- **30s of grace before the first attempt**, so an operator who is awake and
  looking at the printer gets to decide first, and so a pause that clears on
  its own is never touched.
- **Severity gate.** A serious or fatal error (severity <= 2) that is NOT
  allowlisted and NOT a known companion blocks every attempt: the machine may
  be genuinely broken and resuming into that is worse than waiting.
  ``0700_0006`` is such a companion — it appears with ``0700_8006`` every time,
  carries no description, and its severity is inferred by bambuddy's
  ``(attr >> 8) & 0xF`` heuristic rather than stated by the firmware, so
  treating it as an independent fatal fault would block the very case this
  module exists for.

This module is the pure decision core: no I/O, no asyncio, no imports from
main. The loop in main.py executes the returned actions and reports back via
``mark_attempted`` / ``mark_notified``.

State is in-memory. A restart resets the attempt budget, which fails toward
*more* retries rather than fewer; the 30s grace and the severity gate still
apply, and a genuinely stuck printer re-escalates after three more attempts.

Toggles: ``BAMBUDDY_HMS_AUTO_CONTINUE=0`` disables the feature outright (the
pause then behaves exactly as it did before this module existed).
"""

import os
from dataclasses import dataclass

# Bambu severity: 1=fatal, 2=serious, 3=common, 4=info.
_SERIOUS = 2

CONTINUE_NOW = "continue_now"  # send the CONTINUE action, then mark_attempted
GIVE_UP = "give_up"  # budget spent — notify a human, then mark_notified


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_codes(name: str, default: str) -> frozenset[str]:
    return frozenset(c.strip().upper() for c in os.environ.get(name, default).split(",") if c.strip())


def enabled() -> bool:
    return os.environ.get("BAMBUDDY_HMS_AUTO_CONTINUE", "1") != "0"


def auto_continue_codes() -> frozenset[str]:
    return _env_codes("BAMBUDDY_HMS_AUTO_CONTINUE_CODES", "0700_8006")


def companion_codes() -> frozenset[str]:
    """Codes that ride along with an allowlisted fault and must not be read as
    an independent serious error. See the module docstring on 0700_0006."""
    return _env_codes("BAMBUDDY_HMS_AUTO_CONTINUE_COMPANION_CODES", "0700_0006")


@dataclass(frozen=True)
class PausedError:
    """One HMS entry as seen while the printer is paused."""

    short_code: str
    full_code: str
    severity: int
    actions: tuple[str, ...]
    job_id: str | None = None


@dataclass
class Action:
    kind: str
    printer_id: int
    short_code: str
    full_code: str
    job_id: str | None
    attempts: int  # attempts already made, BEFORE the one this action asks for
    max_attempts: int
    paused_for_s: float


@dataclass
class _JobState:
    """Per-print-job bookkeeping. Keyed by the firmware's job_id so the budget
    follows the print, not the individual pause."""

    job_id: str | None
    attempts: int = 0
    last_attempt_at: float = 0.0
    gave_up_notified: bool = False
    paused_since: float | None = None


class HmsAutoContinue:
    def __init__(
        self,
        *,
        delay_s: float | None = None,
        max_attempts: int | None = None,
        settle_s: float | None = None,
    ) -> None:
        # Grace before the first attempt: room for a human to look first.
        self.delay_s = delay_s if delay_s is not None else _env_float("BAMBUDDY_HMS_AUTO_CONTINUE_DELAY_S", 30.0)
        self.max_attempts = (
            max_attempts if max_attempts is not None else _env_int("BAMBUDDY_HMS_AUTO_CONTINUE_MAX_ATTEMPTS", 3)
        )
        # After sending CONTINUE, wait this long before counting the printer's
        # continued PAUSE as another attempt. The A1 lags its state field and
        # the resume itself re-feeds filament, which takes time.
        self.settle_s = settle_s if settle_s is not None else _env_float("BAMBUDDY_HMS_AUTO_CONTINUE_SETTLE_S", 45.0)
        self._jobs: dict[int, _JobState] = {}

    # ------------------------------------------------------------------ tick

    def tick(
        self,
        printer_id: int,
        *,
        now: float,
        connected: bool,
        paused: bool,
        errors: list[PausedError],
    ) -> list[Action]:
        """Decide what to do for one printer this tick.

        ``errors`` is the printer's current HMS list; it is only consulted when
        ``paused`` is true. Returns the actions the caller must execute.
        """
        if not enabled():
            return []

        allow = auto_continue_codes()
        companions = companion_codes()

        candidate = next(
            (e for e in errors if e.short_code in allow and "CONTINUE" in e.actions),
            None,
        )

        # The job the budget belongs to. Fall back to the candidate's job_id;
        # None is a legitimate value (idle-sourced errors) and simply means one
        # shared budget until a real job_id shows up.
        job_id = candidate.job_id if candidate else None
        state = self._jobs.get(printer_id)
        if state is None or (candidate is not None and state.job_id != job_id):
            state = _JobState(job_id=job_id)
            self._jobs[printer_id] = state

        # A disconnected printer's snapshot is not evidence — freeze rather
        # than counting the blackout as a pause.
        if not connected:
            state.paused_since = None
            return []

        if not paused:
            # Resumed (by us, by the operator, or by the printer itself).
            # Keep `attempts` — the budget is per job, so a second pause later
            # in the same print continues counting.
            state.paused_since = None
            return []

        if candidate is None:
            # Paused for something else entirely; not ours to touch.
            state.paused_since = None
            return []

        if state.paused_since is None:
            state.paused_since = now
        paused_for = now - state.paused_since

        # Severity gate: any serious/fatal error that is neither the fault we
        # handle nor a known companion of it means "do not resume into this".
        blockers = [
            e.short_code
            for e in errors
            if e.severity <= _SERIOUS and e.short_code not in allow and e.short_code not in companions
        ]
        if blockers:
            return []

        if paused_for < self.delay_s:
            return []

        if state.attempts >= self.max_attempts:
            if state.gave_up_notified:
                return []
            return [self._action(GIVE_UP, printer_id, candidate, state, paused_for)]

        # Give the previous CONTINUE time to take effect before spending
        # another attempt on it.
        if state.last_attempt_at and now - state.last_attempt_at < self.settle_s:
            return []

        return [self._action(CONTINUE_NOW, printer_id, candidate, state, paused_for)]

    def _action(self, kind: str, printer_id: int, err: PausedError, state: _JobState, paused_for: float) -> Action:
        return Action(
            kind=kind,
            printer_id=printer_id,
            short_code=err.short_code,
            full_code=err.full_code,
            job_id=err.job_id,
            attempts=state.attempts,
            max_attempts=self.max_attempts,
            paused_for_s=paused_for,
        )

    # ------------------------------------------------------------- callbacks

    def mark_attempted(self, printer_id: int, now: float) -> None:
        state = self._jobs.get(printer_id)
        if state is None:
            return
        state.attempts += 1
        state.last_attempt_at = now

    def mark_notified(self, printer_id: int) -> None:
        state = self._jobs.get(printer_id)
        if state is not None:
            state.gave_up_notified = True

    def attempts_for(self, printer_id: int) -> int:
        state = self._jobs.get(printer_id)
        return state.attempts if state else 0

    def forget(self, printer_id: int) -> None:
        self._jobs.pop(printer_id, None)


hms_auto_continue = HmsAutoContinue()
