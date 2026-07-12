"""Regression: the REST status route (used by the frontend) must surface the
filament-spoof tray fields — they were only mapped in the WebSocket
serializer (printer_manager.printer_state_to_dict) initially, leaving the
UI badge dead while the overlay itself worked."""

import re


def test_rest_route_maps_spoof_marker_fields():
    src = open("backend/app/api/routes/printers.py").read()
    # The AMSTray construction in the status route must pass all three fields.
    assert "is_spoofed_backup=_spoof_state is not None" in src
    assert "spoof_state=_spoof_state" in src
    assert "spoof_primary=_spoof_primary" in src
    # And the marker keys must be the frontend contract (ams_id/tray_id).
    m = re.search(r'_spoof_marker\.get\("ams_id"\)', src)
    assert m, "route must read marker keys ams_id/tray_id"
