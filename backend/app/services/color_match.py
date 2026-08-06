"""Perceptual colour comparison for AMS filament matching (#5).

The scheduler decides whether a queue item may dispatch against the trays that
are actually loaded.  Historically that decision compared R, G and B
independently against a fixed ``±40`` window — a *box in RGB space*, not a
colour distance.  Three failures came out of that one shape:

1. one channel could veto the other two, so ``#D6001C`` (pure red) and
   ``#EB3A3A`` (bright red) were called different because green missed by 18;
2. a colour that could not be read was reported as a colour *mismatch*, which
   sends an operator looking for a difference that was never established;
3. the alpha byte was truncated, so translucent and opaque filament of the same
   RGB compared as identical — a job sliced for ``Bambu PETG Translucent``
   (``#FFFFFF00``, ``GFG01``) would dispatch onto an opaque white spool.

This module replaces the box with ΔE in CIELAB, keeps alpha, and gives callers
a way to tell "unreadable" apart from "different".

Why CIELAB and not the alternatives, measured over the nine pairs pinned in
``backend/tests/unit/test_color_match.py``:

- **ΔE76** separates them cleanly: everything that should match is ≤ 11.2,
  everything that must not is ≥ 32.6.  Any threshold in 12–32 is correct for
  all nine; 20 leaves ~1.8x margin below and ~1.6x above.
- **RGB Euclidean** also separates them (68.6 vs 113.4) but with a 1.65x gap
  and no perceptual uniformity, so one constant means different visual
  tolerances in different regions of the space.
- **Cosine similarity** is scale-invariant and therefore discards lightness
  entirely: white vs mid grey scores 1.0000, ``#FF0000`` vs ``#110000`` scores
  1.0000, and black is the zero vector so any comparison with it is undefined.
  That is precisely the class of error the mapping guard exists to prevent (the
  2026-07-19 red-instead-of-black incident).  Recorded here so it is not
  re-proposed.

Everything is overridable by environment variable so a bad threshold can be
corrected on the live service without a deploy, and so the whole change can be
reverted to the previous behaviour per CLAUDE.md's toggle rule:

- ``BAMBUDDY_COLOR_MATCH_MODE=rgb`` restores the per-channel ±40 box.
- ``BAMBUDDY_COLOR_DELTA_E_THRESHOLD`` (default 20.0) widens/narrows ΔE.
- ``BAMBUDDY_COLOR_ALPHA_THRESHOLD`` (default 64, 0-255) sizes the opacity
  test; 255 disables it.
"""

from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)

# D65 white point (X, Y, Z), the reference sRGB is defined against.
_D65 = (0.95047, 1.0, 1.08883)

# ΔE76 in CIELAB.  Swapping in ΔE2000 later is a change to `delta_e()` alone.
DEFAULT_DELTA_E_THRESHOLD = 20.0

# Alpha is 0-255.  Opaque-vs-translucent is a filament difference, not a shade,
# so the window is much tighter than the full range: `#FFFFFF00` (translucent)
# against `#FFFFFFFF` (opaque) must not pass.
DEFAULT_ALPHA_THRESHOLD = 64

# Legacy per-channel window, kept only for BAMBUDDY_COLOR_MATCH_MODE=rgb.
LEGACY_RGB_THRESHOLD = 40

RGBA = tuple[int, int, int, int]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def delta_e_threshold() -> float:
    """Configured ΔE76 ceiling for "the same colour"."""
    return _env_float("BAMBUDDY_COLOR_DELTA_E_THRESHOLD", DEFAULT_DELTA_E_THRESHOLD)


def alpha_threshold() -> float:
    """Configured alpha window (0-255).  255 disables the opacity test."""
    return _env_float("BAMBUDDY_COLOR_ALPHA_THRESHOLD", DEFAULT_ALPHA_THRESHOLD)


def legacy_rgb_mode() -> bool:
    """True when the pre-#5 per-channel box is selected."""
    return os.environ.get("BAMBUDDY_COLOR_MATCH_MODE", "deltae").strip().lower() == "rgb"


def parse_color(color: str | None) -> RGBA | None:
    """``"#RRGGBB"`` / ``"#RRGGBBAA"`` -> ``(r, g, b, alpha)``, else ``None``.

    ``None`` means *unreadable*, which is a different fact from "different" and
    callers are expected to treat it as such.

    An absent alpha is read as opaque (255) rather than as "unknown".  Both
    sides of a real comparison carry alpha: the 3MF writes 8-digit RGBA for
    translucent presets, and the printer reports ``tray_color`` as 8-digit
    RGBA (``EB3A3AFF``).  Treating a missing alpha as unknown, and skipping the
    opacity test whenever either side is 6-digit, would mean the test never
    fires for the shape the data actually takes.
    """
    if not color:
        return None
    h = color.replace("#", "").strip().lower()
    if len(h) not in (6, 8):
        return None
    try:
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
        a = int(h[6:8], 16) if len(h) == 8 else 255
    except ValueError:
        return None
    return r, g, b, a


def normalize_for_compare(color: str | None) -> str:
    """Canonical ``"rrggbbaa"`` for equality, or ``""`` when unreadable.

    Unlike the truncating normaliser this replaces, a 6-digit colour gains an
    explicit ``ff`` rather than losing its alpha, so ``#FFFFFF`` and
    ``#FFFFFFFF`` still compare equal while ``#FFFFFF00`` no longer does.
    """
    rgba = parse_color(color)
    if rgba is None:
        return ""
    return "{:02x}{:02x}{:02x}{:02x}".format(*rgba)


def colors_are_exact(color1: str | None, color2: str | None) -> bool:
    """Exact same colour, alpha included.

    Two *unreadable* colours are not "the same colour" — without this guard a
    tray that reported nothing would exact-match a requirement that specified
    nothing, and the pair would be dispatched as a confirmed match.
    """
    n1 = normalize_for_compare(color1)
    return bool(n1) and n1 == normalize_for_compare(color2)


def srgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """sRGB (0-255, D65) -> CIELAB ``(L*, a*, b*)``."""

    def linear(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(v) for v in rgb)
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    d = 6 / 29

    def f(t: float) -> float:
        return t ** (1 / 3) if t > d**3 else t / (3 * d * d) + 4 / 29

    fx, fy, fz = (f(v / w) for v, w in zip((x, y, z), _D65, strict=True))
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(color1: str | None, color2: str | None) -> float | None:
    """ΔE76 between two colours, ignoring alpha.  ``None`` if either is
    unreadable.  Exposed for logs and diagnostics — the hold reason is much
    easier to act on when it carries the distance that failed."""
    c1, c2 = parse_color(color1), parse_color(color2)
    if c1 is None or c2 is None:
        return None
    return math.dist(srgb_to_lab(c1[:3]), srgb_to_lab(c2[:3]))


def _rgb_box_similar(c1: RGBA, c2: RGBA) -> bool:
    """Pre-#5 behaviour: independent per-channel windows, alpha ignored."""
    return all(abs(a - b) <= LEGACY_RGB_THRESHOLD for a, b in zip(c1[:3], c2[:3], strict=True))


def colors_are_similar(color1: str | None, color2: str | None, threshold: float | None = None) -> bool:
    """Whether two filament colours are close enough to be the same spool.

    Returns ``False`` when either colour is unreadable — callers that need to
    report *why* should ask :func:`parse_color` first, so "I could not read the
    colour" is not rendered as "the colours differ".
    """
    c1, c2 = parse_color(color1), parse_color(color2)
    if c1 is None or c2 is None:
        return False

    if legacy_rgb_mode():
        return _rgb_box_similar(c1, c2)

    if abs(c1[3] - c2[3]) > alpha_threshold():
        return False

    limit = delta_e_threshold() if threshold is None else threshold
    return math.dist(srgb_to_lab(c1[:3]), srgb_to_lab(c2[:3])) <= limit
