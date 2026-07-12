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
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem

logger = logging.getLogger(__name__)

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
