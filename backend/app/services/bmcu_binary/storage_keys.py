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
