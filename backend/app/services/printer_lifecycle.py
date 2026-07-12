"""Single owner of PrintQueueItem.status transitions (strangler-fig facade).

Motivated by the 2026-07-12 BMCU double-feed incident: an unreviewed helper
force-wrote a live "printing" queue item back to "pending" and the printer
received the project twice. Status writes are being migrated here one site at
a time; the AST fence in tests/unit/test_code_quality.py enforces that no new
direct writes appear outside this module.

Two entry points with opposite concurrency polarity:

* :func:`transition` — compare-and-swap. The write is an
  ``UPDATE ... WHERE id = :id AND status IN (:from_states)`` and the rowcount
  decides the outcome. Never python check-then-commit: an await between check
  and commit loses the race to a concurrent session (#1853).
* :func:`force_transition` — unconditional. The name is the warning label;
  use only where clobbering a concurrent transition is the intended semantics
  (e.g. /stop must cancel even when the printer is offline).

Neither swallows DB exceptions — callers that need retry keep using
``run_with_retry`` around the call (the watchdog maps an exception to its
"revert_failed" arm).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gcode-state vocabulary (phase 3) — the single home for the state sets that
# were previously re-declared per judgment site. Polarity on uncertainty is a
# property of each PREDICATE below, not of these sets: the same TERMINAL set
# means "dispatchable" to the scheduler and "job is dead" to the reconcile
# sweep. There is deliberately no single is_busy() — the 2026-07-12 audit
# found three coexisting polarities, and collapsing them into one boolean is
# exactly the bug-shape this module exists to prevent.
# ---------------------------------------------------------------------------

TERMINAL_GCODE_STATES = frozenset({"IDLE", "FINISH", "FAILED"})
# PAUSE is active on purpose — a paused print is still loaded on the bed
# (#1890: cutting power to a paused print ruins it).
ACTIVE_PRINT_STATES = frozenset({"RUNNING", "PAUSE", "PREPARE", "SLICING"})
# PrinterState construction defaults before the first push_status lands
# (#1679) — never evidence of anything.
UNKNOWN_GCODE_STATES = frozenset({"", "UNKNOWN"})


def print_process_active(state: Any | None) -> bool:
    """True when the printer currently has a print loaded / in progress.

    FAIL-OPEN polarity (#1890 smart-plug auto-off): disconnected, no state, or
    unknown state all return False so the plug is allowed to cut power — the
    only safe misread here is "nothing is printing". Do NOT reuse this for
    dispatch or requeue decisions; those need the fail-safe predicates below.
    """
    if not state or not getattr(state, "connected", False):
        return False
    return state.state in ACTIVE_PRINT_STATES


@dataclass(frozen=True)
class IdleVerdict:
    idle: bool
    # Skip-reason for logging when not idle. Empty string on the one silent
    # arm (print visibly started — nothing worth logging) and when idle.
    reason: str = ""

    def __bool__(self) -> bool:
        return self.idle


def evaluate_demonstrably_idle(
    printer_state: Any | None,
    *,
    in_dispatch_hold: Callable[[], bool],
) -> IdleVerdict:
    """FAIL-SAFE idleness for destructive rescues (HMS requeue and friends).

    Any uncertainty means "not idle": requeuing a live job double-sends the
    project file (2026-07-12 BMCU double-feed jam). Gate order is load-bearing
    and mirrors the original _requeue_print_rejected_by_hms stack:

      1. state None/unknown           -> not idle ("printer state unknown")
      2. state not terminal           -> not idle (reason "" — the print
                                          visibly started; callers stay silent)
      3. in_dispatch_hold()           -> not idle ("inside post-dispatch hold";
                                          the wedge watchdog #1678 owns the
                                          dead-or-alive call in that window)
      4. not connected                -> not idle ("printer not connected";
                                          state incl. ams_status_main may be
                                          stale from the dead session)
      5. ams_status_main != 0         -> not idle ("AMS busy (...)"; BMCU is
                                          mid-motion even though gcode_state
                                          reads idle)

    ``in_dispatch_hold`` is a zero-arg callable so the hold lookup (which pops
    expired holds as a side effect) only runs when gates 1-2 pass, exactly as
    the original short-circuit did.
    """
    state = getattr(printer_state, "state", None)
    if state is None or state == "unknown":
        return IdleVerdict(False, "printer state unknown")
    if state not in TERMINAL_GCODE_STATES:
        return IdleVerdict(False, "")
    if in_dispatch_hold():
        return IdleVerdict(False, "inside post-dispatch hold")
    if not getattr(printer_state, "connected", False):
        return IdleVerdict(False, "printer not connected")
    ams_status_main = getattr(printer_state, "ams_status_main", 0)
    if ams_status_main != 0:
        return IdleVerdict(False, f"AMS busy (ams_status_main={ams_status_main})")
    return IdleVerdict(True, "")


def demonstrably_idle(printer_id: int) -> IdleVerdict:
    """ID wrapper over :func:`evaluate_demonstrably_idle` for the live
    singletons. Late imports keep this module import-light (models only) and
    cycle-free — printer_manager and the scheduler both import this module."""
    from backend.app.services.print_scheduler import scheduler
    from backend.app.services.printer_manager import printer_manager

    client = printer_manager.get_client(printer_id)
    return evaluate_demonstrably_idle(
        getattr(client, "state", None),
        in_dispatch_hold=lambda: scheduler.printer_in_dispatch_hold(printer_id),
    )


def active_job_stale(archive_subtask_id: str | None, state: Any) -> tuple[bool, str]:
    """Reconcile-sweep predicate: is an archive in ``status="printing"``
    provably no longer the print on the printer? Returns ``(is_stale, reason)``.

    CONSERVATIVE polarity (#1542 / #1679): degenerate input means "not stale" —
    a real stale archive is caught by the next push_status with terminal state,
    while a false positive would synthesise a spurious "aborted". Triggers:

      1. Terminal printer state (IDLE / FINISH / FAILED) — the print is
         provably not running anymore.
      2. subtask_id mismatch — firmware mints a fresh subtask_id per print
         (including the post-power-cycle ghost replay), so a mismatch
         unambiguously means the archive's print is gone.
      3. Running but empty subtask_name — the printer doesn't know what it's
         running; the archive's reference to it is already broken.

    Pre-push guard: ``""``/``"unknown"`` state is PrinterState construction
    defaults (#1679), not evidence — never stale on those.
    """
    current_state = (state.state or "").upper()
    if current_state in UNKNOWN_GCODE_STATES:
        return False, ""
    if current_state in TERMINAL_GCODE_STATES:
        return True, f"printer state {current_state}"
    # Below here the printer is in a running / pre-running state (RUNNING /
    # PAUSE / PREPARE / SLICING / etc.) — decide based on subtask identity.
    current_subtask_id = (state.subtask_id or "").strip()
    if archive_subtask_id and current_subtask_id and archive_subtask_id != current_subtask_id:
        return True, f"subtask_id changed ({archive_subtask_id!r} → {current_subtask_id!r})"
    current_subtask_name = (state.subtask_name or "").strip()
    if not current_subtask_name:
        return True, "printer subtask_name empty"
    return False, ""

# Columns callers may update atomically together with status. Kept narrow on
# purpose — this facade owns lifecycle state, not generic row editing.
EXTRA_COLUMN_WHITELIST = frozenset({"error_message", "completed_at", "started_at"})


class TransitionOutcome(str, Enum):
    APPLIED = "applied"  # CAS matched (or force applied); row updated
    STATE_MISMATCH = "state_mismatch"  # CAS rowcount == 0: another writer won
    NOT_FOUND = "not_found"  # row no longer exists (e.g. cancel-then-remove)


@dataclass(frozen=True)
class TransitionResult:
    outcome: TransitionOutcome
    item_id: int
    to_status: str
    from_states: tuple[str, ...] | None  # None for force_transition
    observed_status: str | None = None  # best-effort re-read on non-APPLIED

    def __bool__(self) -> bool:
        """Truthy iff APPLIED. NOT_FOUND and STATE_MISMATCH are both falsy —
        inspect ``.outcome`` when the distinction matters."""
        return self.outcome is TransitionOutcome.APPLIED


def _check_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    if not extra:
        return {}
    unknown = set(extra) - EXTRA_COLUMN_WHITELIST
    if unknown:
        raise ValueError(f"extra columns not allowed in a lifecycle transition: {sorted(unknown)}")
    return extra


def _mirror_item(item: PrintQueueItem | None, to_status: str, extra: dict[str, Any]) -> None:
    """Sync a caller-held ORM instance with the values the UPDATE persisted."""
    if item is None:
        return
    item.status = to_status
    for column, value in extra.items():
        setattr(item, column, value)


async def _observe_status(db: AsyncSession, item_id: int) -> str | None:
    row = await db.execute(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id))
    return row.scalar_one_or_none()


async def _execute(
    db: AsyncSession,
    item_id: int,
    *,
    to_status: str,
    from_states: tuple[str, ...] | None,
    reason: str,
    caller: str,
    extra: dict[str, Any] | None,
    item: PrintQueueItem | None,
    commit: bool,
) -> TransitionResult:
    extra = _check_extra(extra)
    stmt = update(PrintQueueItem).where(PrintQueueItem.id == item_id).values(status=to_status, **extra)
    if from_states is not None:
        stmt = stmt.where(PrintQueueItem.status.in_(from_states))
    result = await db.execute(stmt)

    if result.rowcount != 0:
        if commit:
            await db.commit()
        _mirror_item(item, to_status, extra)
        outcome = TransitionResult(TransitionOutcome.APPLIED, item_id, to_status, from_states)
    else:
        observed = await _observe_status(db, item_id)
        kind = TransitionOutcome.NOT_FOUND if observed is None else TransitionOutcome.STATE_MISMATCH
        outcome = TransitionResult(kind, item_id, to_status, from_states, observed_status=observed)

    from_repr = "|".join(from_states) if from_states is not None else "FORCE"
    log = logger.info if outcome else logger.warning
    log(
        "PQ_LIFECYCLE item=%s %s->%s outcome=%s reason=%s caller=%s",
        item_id,
        from_repr,
        to_status,
        outcome.outcome.value,
        reason,
        caller,
    )
    return outcome


async def transition(
    db: AsyncSession,
    item_id: int,
    *,
    to_status: str,
    from_states: tuple[str, ...],
    reason: str,
    caller: str,
    extra: dict[str, Any] | None = None,
    item: PrintQueueItem | None = None,
    commit: bool = True,
) -> TransitionResult:
    """CAS status transition: applies only if the row is still in from_states.

    ``commit=True`` (default) commits immediately — an uncommitted CAS is not
    visible to concurrent sessions and reopens the #1853 window. Pass
    ``commit=False`` only when batching several writes into one caller-owned
    commit, and commit promptly.

    On success, a caller-held ``item`` is mirrored to the persisted values.
    On STATE_MISMATCH the instance is left untouched (it is stale);
    ``observed_status`` carries a best-effort re-read, and callers needing the
    full fresh row should ``await db.refresh(item)`` themselves.
    """
    if not from_states:
        raise ValueError("from_states must be non-empty; use force_transition for unconditional writes")
    return await _execute(
        db,
        item_id,
        to_status=to_status,
        from_states=tuple(from_states),
        reason=reason,
        caller=caller,
        extra=extra,
        item=item,
        commit=commit,
    )


async def force_transition(
    db: AsyncSession,
    item_id: int,
    *,
    to_status: str,
    reason: str,
    caller: str,
    extra: dict[str, Any] | None = None,
    item: PrintQueueItem | None = None,
    commit: bool = True,
) -> TransitionResult:
    """Unconditional status write — clobbers concurrent transitions by design.

    Reach for this only where that is the documented intent (see the
    lifecycle-polarity comments at the call sites); everything else should
    state its expectations via :func:`transition`.
    """
    return await _execute(
        db,
        item_id,
        to_status=to_status,
        from_states=None,
        reason=reason,
        caller=caller,
        extra=extra,
        item=item,
        commit=commit,
    )
