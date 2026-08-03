"""Row sampling for range queries over BMB1 telemetry.

A chart asks for a time window and can render a bounded number of rows. Taking
the first N rows of the window answers with the oldest slice and never reaches
the present, which is what the BMCU Link range buttons used to do. Taking the
newest N answers with the present but covers minutes of a multi-hour window.
Both are wrong for the same reason: the bound must thin the window out, not cut
it short.

Thinning uniformly is also not enough here. The stored kinds differ in rate by
orders of magnitude — over a recent 48 h retention window this device stored
about 55,000 BMCU EVENT frames against 847 STATUS frames — so a single stride
across all rows would spend the whole budget on the loudest kind and drop the
rare frames that carry loader state. The budget is therefore split max-min
fairly between kinds first: a kind that fits inside its share is kept whole and
its unused share is redistributed, so only genuinely busy kinds get strided.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping


def fair_shares(counts: Mapping[Hashable, int], budget: int) -> dict[Hashable, int]:
    """Max-min fair split of ``budget`` across ``counts``.

    Every key gets at least one row, so a large key set may exceed the budget by
    at most one row per key. Callers keep a hard ``LIMIT`` for that.
    """
    shares: dict[Hashable, int] = {}
    pending = {key: value for key, value in counts.items() if value > 0}
    while pending:
        share = max(1, budget // len(pending))
        satisfied = [key for key, value in pending.items() if value <= share]
        if not satisfied:
            shares.update(dict.fromkeys(pending, share))
            break
        for key in satisfied:
            shares[key] = pending.pop(key)
            budget -= shares[key]
    return shares


def sample_strides(counts: Mapping[Hashable, int], budget: int) -> dict[Hashable, int]:
    """Per-key stride that keeps the window spanned within ``budget`` rows.

    A stride of one means "keep every row of this key". Rows are selected by
    ``(row_number - 1) % stride == 0`` so the first row of each key survives and
    the kept rows stay evenly spread over the window.
    """
    if budget <= 0:
        raise ValueError("budget must be positive")
    shares = fair_shares(counts, budget)
    return {key: max(1, -(-value // max(1, shares.get(key, 1)))) for key, value in counts.items() if value > 0}
