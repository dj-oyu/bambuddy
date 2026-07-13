"""Level-triggered retry state machine for HMS auto-clear (private fork).

Why this exists (2026-07-13 incident): the BMCU raises 0500_409D at print
start and the printer rejects the job while staying IDLE. The old auto-clear
was edge-triggered (fired only when the code *newly appeared*) with a
3-attempts/600s rate limit — after the limit tripped, the code stayed latched
in the HMS list, never counted as "new" again, and the queue item sat in
'printing' forever with no notification (auto-handled codes are suppressed
from the per-occurrence HMS notification path).

Design (user-approved 2026-07-13):
  - Level trigger: while a configured code remains present, keep retrying
    clear+requeue on an exponential backoff (60/300/900/1800s cap). Never
    give up permanently.
  - Success = the printer actually reaches an active print state AND the code
    is gone — NOT "the clear command returned OK" (every clear "succeeded"
    during the incident while every dispatch was rejected).
  - Escalation: after ESCALATE_AFTER consecutive failures, tell a human
    (Discord). Re-notify every RENOTIFY_EVERY further failures so a single
    missed message at night doesn't mean silence forever. Retries continue
    in the background; recovery sends one final notification.
  - Power / connectivity: a disconnected printer freezes its episodes — a
    dead session's HMS snapshot is not evidence, and counting failures while
    the user power-cycles the BMCU would escalate spuriously. The
    ``connected`` input is the extension point for smart-plug power state
    (Tapo plan): powered-off will feed in as connected=False.
  - Severity: retrying means clearing errors and re-sending a print. If a
    NON-allowlisted serious/fatal error (severity <= 2) is co-present, or an
    allowlisted code reports a worse severity than when the episode started,
    the machine may be genuinely broken — attempts are BLOCKED and a human is
    notified immediately instead of blind-firing jobs into it.

This module is the pure decision core: no I/O, no asyncio, no imports from
main. The retry loop in main.py executes the returned actions (clear command,
requeue, notifications) and reports back via mark_attempted/mark_notified.

State is in-memory by design. Acceptable restart losses: failure counter and
notification latch reset (worst case one duplicate escalation ~3 backoffs
after restart, and the first post-restart attempt fires immediately). The
queue item is 'pending' in the DB so recovery itself survives restarts, and
the level trigger re-detects codes already present at boot — an improvement
over the edge trigger, which missed them entirely.
"""

import os
from dataclasses import dataclass, field


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_backoff(name: str, default: tuple[float, ...]) -> tuple[float, ...]:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        parsed = tuple(float(p) for p in raw.split(",") if p.strip())
        return parsed or default
    except ValueError:
        return default


# Action kinds returned by tick()
ATTEMPT = "attempt"        # run clear+requeue now, then call mark_attempted
ESCALATE = "escalate"      # send/refresh the human notification, then mark_notified
RECOVERED = "recovered"    # send the recovery notification (episode already closed)


@dataclass
class Action:
    kind: str
    printer_id: int
    code: str
    failures: int = 0
    blocked_reason: str = ""
    since: float = 0.0
    # RECOVERED only: whether a print was actually running at close time.
    # False = the code just went away with the printer idle (manual fix /
    # empty queue) — the notification must not claim "printing again".
    print_active: bool = False


@dataclass
class _Episode:
    since: float
    baseline_severity: int  # severity when first observed (4=info .. 1=fatal)
    failures: int = 0
    next_attempt_at: float = 0.0     # <= now means "may attempt"
    attempt_pending: bool = False
    attempt_deadline: float = 0.0    # settle window end for the pending attempt
    code_present: bool = True        # as of the last connected tick
    blocked_reason: str = ""         # non-empty = attempts held (severity gate)
    # Failure count at the last escalation actually sent; None = never sent.
    notified_at_failures: int | None = None
    blocked_notified: bool = False
    # True while the printer is disconnected; used to re-arm the settle window
    # on reconnect instead of counting the blackout as a failed attempt.
    frozen: bool = False
    # Non-persistent bookkeeping: codes seen absent while waiting (grace).
    absent_since: float | None = field(default=None)


class HmsRetryTracker:
    def __init__(
        self,
        *,
        backoff_s: tuple[float, ...] | None = None,
        settle_s: float | None = None,
        escalate_after: int | None = None,
        renotify_every: int | None = None,
        absent_grace_s: float | None = None,
    ) -> None:
        self.backoff_s = backoff_s or _env_backoff(
            "BAMBUDDY_HMS_RETRY_BACKOFF_S", (60.0, 300.0, 900.0, 1800.0)
        )
        # How long after an attempt we wait for the printer to visibly start
        # printing before counting a failure (A1 state lag + 30s scheduler
        # tick + filament load).
        self.settle_s = settle_s if settle_s is not None else _env_float(
            "BAMBUDDY_HMS_RETRY_SETTLE_S", 150.0
        )
        self.escalate_after = escalate_after if escalate_after is not None else _env_int(
            "BAMBUDDY_HMS_RETRY_ESCALATE_AFTER", 3
        )
        self.renotify_every = renotify_every if renotify_every is not None else _env_int(
            "BAMBUDDY_HMS_RETRY_RENOTIFY_EVERY", 3
        )
        # A code that vanishes must stay gone this long (with no active print
        # required) before the episode closes — 409D flickers off after a
        # clear and back on at the next dispatch.
        self.absent_grace_s = absent_grace_s if absent_grace_s is not None else _env_float(
            "BAMBUDDY_HMS_RETRY_ABSENT_GRACE_S", 180.0
        )
        # {(printer_id, short_code): _Episode}
        self._episodes: dict[tuple[int, str], _Episode] = {}

    # ------------------------------------------------------------------ tick

    def tick(
        self,
        printer_id: int,
        *,
        now: float,
        connected: bool,
        print_active: bool,
        autoclear_present: dict[str, int],
        other_serious: list[str],
    ) -> list[Action]:
        """Evaluate one printer observation.

        autoclear_present: {short_code: severity} for allowlisted codes in the
        current HMS list. other_serious: human-readable descriptions of
        NON-allowlisted severity<=2 errors currently present.
        Returns actions for the caller to execute, in order.
        """
        actions: list[Action] = []

        if not connected:
            # Freeze: stale HMS state is not evidence, and the blackout must
            # not consume the settle window or the backoff clock.
            for key, ep in self._episodes.items():
                if key[0] == printer_id:
                    ep.frozen = True
            return actions

        # Open episodes for newly-present codes (level trigger: also catches
        # codes already latched when bambuddy starts).
        for code, severity in autoclear_present.items():
            key = (printer_id, code)
            if key not in self._episodes:
                self._episodes[key] = _Episode(
                    since=now, baseline_severity=severity, next_attempt_at=now
                )

        for key in [k for k in self._episodes if k[0] == printer_id]:
            code = key[1]
            ep = self._episodes[key]
            severity = autoclear_present.get(code)
            present = severity is not None

            if ep.frozen:
                ep.frozen = False
                if ep.attempt_pending:
                    # Fresh settle window: don't count the blackout against
                    # the attempt (the user may have just power-cycled).
                    ep.attempt_deadline = now + self.settle_s
                ep.absent_since = None

            ep.code_present = present

            # ---- resolution: code gone -------------------------------------
            if not present:
                if print_active:
                    actions.extend(self._close(key, ep, print_active=True))
                    continue
                if ep.absent_since is None:
                    ep.absent_since = now
                if ep.attempt_pending and now < ep.attempt_deadline:
                    continue  # give the dispatch time to start (or re-raise)
                if now - ep.absent_since >= self.absent_grace_s:
                    # Gone and stayed gone with nothing printing — resolved
                    # outside our loop (manual fix, or no job queued).
                    actions.extend(self._close(key, ep, print_active=False))
                continue
            ep.absent_since = None

            # ---- severity gate ----------------------------------------------
            blocked = ""
            if other_serious:
                blocked = "serious/fatal HMS co-present: " + "; ".join(other_serious[:3])
            elif severity < ep.baseline_severity:
                blocked = (
                    f"severity worsened ({ep.baseline_severity} -> {severity})"
                )
            if blocked:
                ep.blocked_reason = blocked
                ep.attempt_pending = False
                if not ep.blocked_notified:
                    actions.append(
                        Action(ESCALATE, printer_id, code, ep.failures, blocked, ep.since)
                    )
                continue
            if ep.blocked_reason:
                # Unblocked: allow an attempt now; re-arm block notification.
                ep.blocked_reason = ""
                ep.blocked_notified = False
                ep.next_attempt_at = min(ep.next_attempt_at, now)

            # ---- pending attempt: settle or fail -----------------------------
            if ep.attempt_pending:
                if now < ep.attempt_deadline:
                    continue
                ep.attempt_pending = False
                ep.failures += 1
                step = self.backoff_s[min(ep.failures - 1, len(self.backoff_s) - 1)]
                ep.next_attempt_at = now + step
                if self._should_escalate(ep):
                    actions.append(
                        Action(ESCALATE, printer_id, code, ep.failures, "", ep.since)
                    )
                continue

            # ---- idle episode: attempt when backoff expires ------------------
            if now >= ep.next_attempt_at:
                actions.append(
                    Action(ATTEMPT, printer_id, code, ep.failures, "", ep.since)
                )
            elif self._should_escalate(ep):
                # Escalation owed but the last send failed — retry it even
                # while waiting out the backoff.
                actions.append(
                    Action(ESCALATE, printer_id, code, ep.failures, "", ep.since)
                )

        return actions

    def _should_escalate(self, ep: _Episode) -> bool:
        if ep.failures < self.escalate_after:
            return False
        if ep.notified_at_failures is None:
            return True
        return ep.failures - ep.notified_at_failures >= self.renotify_every

    def _close(self, key: tuple[int, str], ep: _Episode, *, print_active: bool) -> list[Action]:
        del self._episodes[key]
        if ep.notified_at_failures is not None or ep.blocked_notified:
            return [Action(RECOVERED, key[0], key[1], ep.failures, "", ep.since, print_active)]
        return []

    # ------------------------------------------------------------- callbacks

    def mark_attempted(self, printer_id: int, code: str, now: float) -> None:
        ep = self._episodes.get((printer_id, code))
        if ep is not None:
            ep.attempt_pending = True
            ep.attempt_deadline = now + self.settle_s

    def mark_notified(self, printer_id: int, code: str) -> None:
        """Latch AFTER a successful send (stall-watch pattern): a failed send
        retries on the next tick instead of dropping the alert."""
        ep = self._episodes.get((printer_id, code))
        if ep is None:
            return
        if ep.blocked_reason:
            ep.blocked_notified = True
        else:
            ep.notified_at_failures = ep.failures

    # -------------------------------------------------------- scheduler gate

    def dispatch_allowed(self, printer_id: int, now: float) -> bool:
        """False only while a code is currently present AND we are deliberately
        waiting (backoff or severity block). FAIL-PERMISSIVE: no episode,
        code absent, pending attempt (the dispatch IS the retry) all allow —
        a stale episode must never wedge the queue.

        Iterating _episodes without a copy is safe only because tick() (the
        sole mutator) and this gate both run on the single event loop and
        never interleave mid-iteration."""
        for (pid, _code), ep in self._episodes.items():
            if pid != printer_id or not ep.code_present:
                continue
            if ep.blocked_reason:
                return False
            if not ep.attempt_pending and now < ep.next_attempt_at:
                return False
        return True

    def episode_info(self, printer_id: int, code: str) -> _Episode | None:
        return self._episodes.get((printer_id, code))


hms_retry = HmsRetryTracker()
