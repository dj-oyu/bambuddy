"""Budget splitting for windowed BMCU range queries."""

from __future__ import annotations

import pytest

from backend.app.services.bmcu_binary.sampling import fair_shares, sample_strides


def test_a_kind_that_fits_its_share_is_kept_whole() -> None:
    strides = sample_strides({"status": 10, "event": 10_000}, 1000)
    assert strides["status"] == 1


def test_the_loud_kind_cannot_starve_the_rare_one() -> None:
    """The live ratio: EVENT outnumbers STATUS by about 65 to 1 over a 48 h
    window, and a single stride across both would keep almost no STATUS."""
    counts = {2: 847, 3: 55_300}
    strides = sample_strides(counts, 1000)
    kept = {kind: -(-count // strides[kind]) for kind, count in counts.items()}
    naive_stride = -(-sum(counts.values()) // 1000)
    assert -(-counts[2] // naive_stride) < 20  # one stride for both keeps ~15 STATUS
    assert kept[2] >= 400
    assert kept[3] >= 400
    assert sum(kept.values()) <= 1000 + len(counts)


def test_unused_share_is_redistributed_not_wasted() -> None:
    shares = fair_shares({"a": 1, "b": 1, "c": 600}, 300)
    assert (shares["a"], shares["b"]) == (1, 1)
    assert shares["c"] == 298


def test_budget_below_the_kind_count_still_keeps_one_row_each() -> None:
    counts = dict.fromkeys(range(10), 100)
    strides = sample_strides(counts, 3)
    assert all(stride == 100 for stride in strides.values())


def test_zero_count_kinds_are_dropped_from_the_split() -> None:
    assert sample_strides({"a": 0, "b": 10}, 10) == {"b": 1}


def test_empty_window_has_no_strides() -> None:
    assert sample_strides({}, 100) == {}


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError):
        sample_strides({"a": 1}, 0)
