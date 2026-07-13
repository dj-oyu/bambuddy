"""get_error_description_full — disambiguates short-scheme collisions.

The MMMM_EEEE short code drops attr's low 16 bits and code's high 16 bits, so
e.g. "runout, purging" (0700_2000_0003_0001) and "cutter sensor malfunction"
(0700_4500_0002_0001) both collapse onto 0700_0001 and resolved to None.
"""

from backend.app.services.hms_errors import (
    get_error_description,
    get_error_description_full,
)


class TestFullCodeLookup:
    def test_runout_purging_resolves(self):
        desc = get_error_description_full(0x07002000, 0x00030001)
        assert desc is not None and "run out" in desc and "purged" in desc

    def test_cutter_sensor_resolves_differently_from_runout(self):
        desc = get_error_description_full(0x07004500, 0x00020001)
        assert desc is not None and "cutter sensor" in desc.lower()

    def test_ams_firmware_mismatch_resolves(self):
        desc = get_error_description_full(0x05000400, 0x00010044)
        assert desc is not None and "firmware" in desc.lower()

    def test_short_scheme_collision_documented(self):
        # The colliding short code has no entry — full lookup is required.
        assert get_error_description("0700_0001") is None

    def test_fallback_to_short_table(self):
        # 0300_800B exists only in the short table.
        desc = get_error_description_full(0x0300FFFF, 0x0000800B)
        assert desc == get_error_description("0300_800B") is not None

    def test_unknown_everywhere_returns_none(self):
        assert get_error_description_full(0x0BAD0000, 0x0000BEEF) is None
