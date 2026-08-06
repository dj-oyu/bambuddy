"""Perceptual colour comparison for AMS matching (#5).

Three bugs shared one shape — a per-channel ±40 box in RGB space:

1. a single drifting channel vetoed the other two, holding jobs whose colour
   was, to the eye, the one they asked for;
2. an unreadable colour was reported as a colour *mismatch*;
3. alpha was truncated, so translucent and opaque filament of the same RGB
   compared as identical.

The table below is the load-bearing test: it pins both the fix and the
regressions the guard must keep catching (the 2026-07-19 red-instead-of-black
incident is the fourth row). ΔE76 separates the two groups with a wide margin —
everything that should match is ≤ 11.2, everything that must not is ≥ 40.3 —
so the 20.0 default sits between them with room on both sides.
"""

import pytest

from backend.app.services import color_match
from backend.app.services.color_match import (
    colors_are_exact,
    colors_are_similar,
    delta_e,
    normalize_for_compare,
    parse_color,
)

# (name, colour a, colour b, should_match)
COLOUR_PAIRS = [
    # -- should match ---------------------------------------------------
    # The live case: queue items 224/225 held against the loaded red PETG.
    # Green misses by 58 (outside ±40) while red and blue are inside, so one
    # channel vetoed two. ΔE76 = 11.2 — both are red, differing in depth.
    ("pure red vs bright red", "#D6001C", "#EB3A3A", True),
    ("white vs off-white", "#FFFFFF", "#F5F5F5", True),
    ("black vs near-black", "#000000", "#0A0A0A", True),
    # -- must NOT match -------------------------------------------------
    # 2026-07-19: a black part dispatched onto the red tray.
    ("red vs black", "#FF0000", "#000000", False),
    # Cosine similarity scores these 1.0000 (identical direction (1,1,1)) —
    # the whole greyscale axis collapses to one point. Pinned so cosine is
    # not re-proposed.
    ("white vs mid grey", "#FFFFFF", "#808080", False),
    ("white vs black", "#FFFFFF", "#000000", False),
    # Cosine also scores this 1.0000; black is the zero vector, so cosine is
    # undefined for any pair involving it.
    ("bright red vs near-black red", "#FF0000", "#110000", False),
    ("grey vs black", "#808080", "#000000", False),
    ("red vs orange", "#FF0000", "#FF8000", False),
]


class TestColourTable:
    @pytest.mark.parametrize("name,a,b,expected", COLOUR_PAIRS, ids=[c[0] for c in COLOUR_PAIRS])
    def test_pair(self, name, a, b, expected):
        assert colors_are_similar(a, b) is expected
        assert colors_are_similar(b, a) is expected, "comparison must be symmetric"

    def test_threshold_sits_between_the_two_groups(self):
        """The margin is the point: a threshold anywhere in this gap is
        correct for all nine pairs, so the default is not balanced on a knife
        edge. Fails loudly if a future ΔE formula change narrows the gap."""
        should = [delta_e(a, b) for _, a, b, ok in COLOUR_PAIRS if ok]
        should_not = [delta_e(a, b) for _, a, b, ok in COLOUR_PAIRS if not ok]
        assert max(should) < color_match.DEFAULT_DELTA_E_THRESHOLD < min(should_not)
        assert max(should) < 12.0
        assert min(should_not) > 32.0


class TestAlpha:
    """Translucent vs opaque is a filament difference, not a shade. Bambu
    Studio writes translucent presets as alpha 00 — a live 3MF required
    ``#FFFFFF00`` with ``tray_info_idx="GFG01"`` (Bambu PETG Translucent)."""

    def test_translucent_does_not_match_six_digit_opaque(self):
        # The 6-digit form is what a hand-entered override and the pre-#5
        # display value look like; testing only the 8-digit pair would pass
        # while this case still slipped through.
        assert colors_are_similar("#FFFFFF00", "#FFFFFF") is False

    def test_translucent_does_not_match_explicit_opaque(self):
        assert colors_are_similar("#FFFFFF00", "#FFFFFFFF") is False

    def test_two_translucents_match(self):
        assert colors_are_similar("#FFFFFF00", "#FFFFFF10") is True

    def test_six_digit_and_eight_digit_opaque_match(self):
        assert colors_are_similar("#D6001C", "#D6001CFF") is True
        assert colors_are_exact("#D6001C", "#D6001CFF") is True

    def test_alpha_test_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("BAMBUDDY_COLOR_ALPHA_THRESHOLD", "255")
        assert colors_are_similar("#FFFFFF00", "#FFFFFFFF") is True


class TestUnreadable:
    """ "I could not read the colour" is not "the colours differ"."""

    @pytest.mark.parametrize("value", [None, "", "#", "#FFF", "#FFFFF", "#GGGGGG", "#FFFFFFFFF", "not a colour"])
    def test_unparseable(self, value):
        assert parse_color(value) is None
        assert normalize_for_compare(value) == ""
        assert colors_are_similar(value, "#FF0000") is False
        assert colors_are_similar("#FF0000", value) is False
        assert delta_e(value, "#FF0000") is None

    def test_two_unknowns_are_not_an_exact_match(self):
        """Without this guard a tray that reported nothing exact-matches a
        requirement that specified nothing, and the pair is dispatched as a
        confirmed match."""
        assert colors_are_exact(None, None) is False
        assert colors_are_exact("", "") is False

    def test_hash_and_case_are_irrelevant(self):
        assert parse_color("d6001c") == parse_color("#D6001C") == (214, 0, 28, 255)


class TestNormalizeForCompare:
    def test_six_digit_gains_explicit_alpha(self):
        assert normalize_for_compare("#FF5500") == "ff5500ff"

    def test_alpha_is_kept_not_truncated(self):
        assert normalize_for_compare("#FF5500AA") == "ff5500aa"

    def test_opaque_forms_are_equal(self):
        assert normalize_for_compare("#FF5500") == normalize_for_compare("ff5500ff")

    def test_translucent_is_not_equal_to_opaque(self):
        assert normalize_for_compare("#FFFFFF00") != normalize_for_compare("#FFFFFF")


class TestEnvOverrides:
    def test_legacy_rgb_mode_restores_the_box(self, monkeypatch):
        monkeypatch.setenv("BAMBUDDY_COLOR_MATCH_MODE", "rgb")
        # The regression this whole issue is about, back again under the toggle.
        assert colors_are_similar("#D6001C", "#EB3A3A") is False
        # ...and alpha ignored again, which is the other half of the old shape.
        assert colors_are_similar("#FFFFFF00", "#FFFFFF") is True

    def test_legacy_mode_still_catches_the_incident(self, monkeypatch):
        monkeypatch.setenv("BAMBUDDY_COLOR_MATCH_MODE", "rgb")
        assert colors_are_similar("#FF0000", "#000000") is False

    def test_delta_e_threshold_override(self, monkeypatch):
        monkeypatch.setenv("BAMBUDDY_COLOR_DELTA_E_THRESHOLD", "5")
        assert colors_are_similar("#D6001C", "#EB3A3A") is False
        monkeypatch.setenv("BAMBUDDY_COLOR_DELTA_E_THRESHOLD", "50")
        assert colors_are_similar("#FFFFFF", "#808080") is True

    def test_garbage_threshold_falls_back_to_the_default(self, monkeypatch):
        """A typo in a systemd drop-in must not crash the scheduler tick."""
        monkeypatch.setenv("BAMBUDDY_COLOR_DELTA_E_THRESHOLD", "twenty")
        assert color_match.delta_e_threshold() == color_match.DEFAULT_DELTA_E_THRESHOLD
        assert colors_are_similar("#D6001C", "#EB3A3A") is True

    def test_explicit_threshold_argument_wins(self):
        assert colors_are_similar("#D6001C", "#EB3A3A", threshold=5) is False


class TestLab:
    def test_reference_values(self):
        """Pinned against the CIELAB definition so a refactor of the transfer
        function or the white point is caught here rather than by a queue that
        silently stops matching."""
        L, a, b = color_match.srgb_to_lab((255, 255, 255))
        assert (round(L, 2), round(a, 2), round(b, 2)) == (100.0, 0.0, 0.0)
        L, a, b = color_match.srgb_to_lab((0, 0, 0))
        assert (round(L, 2), round(a, 2), round(b, 2)) == (0.0, 0.0, 0.0)
        L, a, b = color_match.srgb_to_lab((255, 0, 0))
        assert (round(L, 1), round(a, 1), round(b, 1)) == (53.2, 80.1, 67.2)

    def test_identical_colours_have_zero_distance(self):
        assert delta_e("#123456", "#123456") == 0.0
