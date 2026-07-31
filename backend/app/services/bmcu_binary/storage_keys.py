"""Exact, order-preserving SQL representations for unsigned wire integers."""


def u64_decimal(value: int) -> str:
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("value is outside u64")
    return f"{value:020d}"


def u64_hex(value: int) -> str:
    if not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("value is outside u64")
    return f"{value:016x}"


def advance_contiguous(watermark: int, stored_sequences) -> int:
    """Advance over an ordered sequence iterable without crossing a gap."""
    for stored in stored_sequences:
        candidate = int(stored)
        if candidate <= watermark:
            continue
        if candidate != watermark + 1:
            break
        watermark = candidate
    return watermark


def advance_contiguous_with_losses(watermark: int, stored_sequences, loss_ranges) -> int:
    records = iter(sorted(int(value) for value in stored_sequences if int(value) > watermark))
    losses = sorted((int(first), int(last)) for first, last in loss_ranges)
    candidate = next(records, None)
    while True:
        next_value = watermark + 1
        covering = next(((first, last) for first, last in losses if first <= next_value <= last), None)
        if covering is not None:
            watermark = covering[1]
            continue
        if candidate is not None and candidate <= watermark:
            candidate = next(records, None)
            continue
        if candidate == next_value:
            watermark = candidate
            candidate = next(records, None)
            continue
        return watermark
