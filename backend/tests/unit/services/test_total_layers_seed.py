"""Tests for the A1-mini total_layers seed + pushall fallback.

A1 mini firmware omits `total_layer_num` from incremental MQTT report
pushes (it only appears in pushall responses), so `state.total_layers`
stays 0 for the whole print and the last-layer finish-photo trigger never
fires. The workaround:

- `seed_total_layers()` lets the dispatch path provide the slicer's total
  from the 3MF gcode header; applied after the print-start reset.
- While RUNNING with total_layers==0, a pushall is re-requested at most
  every ~120s until a positive value arrives.
"""

import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mqtt_client():
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TEST123",
        access_code="12345678",
    )
    return client


class TestSeedTotalLayers:
    def test_seed_applies_when_no_mqtt_value(self, mqtt_client):
        mqtt_client.state.total_layers = 0
        mqtt_client.seed_total_layers(80)
        assert mqtt_client.state.total_layers == 80
        assert mqtt_client._pending_total_layers_seed == 80

    def test_seed_does_not_overwrite_mqtt_value(self, mqtt_client):
        mqtt_client.state.total_layers = 120  # already set from MQTT
        mqtt_client.seed_total_layers(80)
        assert mqtt_client.state.total_layers == 120

    def test_seed_ignores_non_positive(self, mqtt_client):
        mqtt_client.state.total_layers = 0
        mqtt_client.seed_total_layers(0)
        mqtt_client.seed_total_layers(-5)
        assert mqtt_client.state.total_layers == 0
        assert mqtt_client._pending_total_layers_seed == 0

    def test_pending_seed_survives_print_start_reset(self, mqtt_client):
        """The print-start handler resets total_layers to 0; the pending
        seed must be applied after that reset (race safety)."""
        mqtt_client.on_print_start = lambda data: None
        mqtt_client._was_running = False
        mqtt_client._previous_gcode_state = "IDLE"

        mqtt_client.seed_total_layers(80)

        payload = {
            "print": {
                "gcode_state": "RUNNING",
                "gcode_file": "/data/Metadata/test_print.gcode",
                "subtask_name": "Test_Print",
                "mc_percent": 0,
            }
        }
        mqtt_client._process_message(payload)

        assert mqtt_client.state.total_layers == 80
        # Consumed — must not bleed into a later print
        assert mqtt_client._pending_total_layers_seed == 0

    def test_mqtt_total_layer_num_overrides_seed(self, mqtt_client):
        mqtt_client.seed_total_layers(80)
        payload = {"print": {"total_layer_num": 95}}
        mqtt_client._process_message(payload)
        assert mqtt_client.state.total_layers == 95
        assert mqtt_client._pending_total_layers_seed == 0


class TestPushallFallback:
    def _running_payload(self):
        return {
            "print": {
                "gcode_state": "RUNNING",
                "gcode_file": "/data/Metadata/test_print.gcode",
                "subtask_name": "Test_Print",
                "mc_percent": 10,
            }
        }

    def test_pushall_requested_when_running_without_total(self, mqtt_client):
        mqtt_client._request_push_all = MagicMock()
        mqtt_client._was_running = True  # mid-print, no new-print reset path
        mqtt_client._previous_gcode_state = "RUNNING"
        mqtt_client.state.total_layers = 0

        mqtt_client._process_message(self._running_payload())
        assert mqtt_client._request_push_all.call_count == 1

    def test_pushall_rate_limited_to_120s(self, mqtt_client):
        mqtt_client._request_push_all = MagicMock()
        mqtt_client._was_running = True
        mqtt_client._previous_gcode_state = "RUNNING"
        mqtt_client.state.total_layers = 0

        mqtt_client._process_message(self._running_payload())
        mqtt_client._process_message(self._running_payload())
        assert mqtt_client._request_push_all.call_count == 1

        # Simulate 120s elapsing since the first request
        mqtt_client._total_layers_pushall_time = time.monotonic() - 121.0
        mqtt_client._process_message(self._running_payload())
        assert mqtt_client._request_push_all.call_count == 2

    def test_pushall_stops_once_total_known(self, mqtt_client):
        mqtt_client._request_push_all = MagicMock()
        mqtt_client._was_running = True
        mqtt_client._previous_gcode_state = "RUNNING"
        mqtt_client.state.total_layers = 80

        mqtt_client._process_message(self._running_payload())
        assert mqtt_client._request_push_all.call_count == 0

    def test_no_pushall_when_not_running(self, mqtt_client):
        mqtt_client._request_push_all = MagicMock()
        mqtt_client.state.total_layers = 0
        payload = {"print": {"gcode_state": "IDLE"}}
        mqtt_client._process_message(payload)
        assert mqtt_client._request_push_all.call_count == 0


class TestExtractTotalLayersFrom3mf:
    def _make_3mf(self, tmp_path, gcode_name="Metadata/plate_1.gcode", total=80):
        import zipfile

        header = (
            "; HEADER_BLOCK_START\n"
            "; BambuStudio 01.09.00.70\n"
            f"; total layer number: {total}\n"
            "; total filament weight [g] : 12.34\n"
            "; HEADER_BLOCK_END\n"
            "G28\n"
        )
        path = tmp_path / "test.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(gcode_name, header)
        return path

    def test_extracts_total_layers(self, tmp_path):
        from backend.app.utils.threemf_tools import extract_total_layers_from_3mf

        path = self._make_3mf(tmp_path, total=80)
        assert extract_total_layers_from_3mf(path, plate_id=1) == 80

    def test_falls_back_to_first_gcode_when_plate_missing(self, tmp_path):
        from backend.app.utils.threemf_tools import extract_total_layers_from_3mf

        path = self._make_3mf(tmp_path, gcode_name="Metadata/plate_2.gcode", total=42)
        assert extract_total_layers_from_3mf(path, plate_id=1) == 42

    def test_returns_none_without_gcode(self, tmp_path):
        import zipfile

        from backend.app.utils.threemf_tools import extract_total_layers_from_3mf

        path = tmp_path / "empty.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Metadata/whatever.txt", "hi")
        assert extract_total_layers_from_3mf(path, plate_id=1) is None

    def test_returns_none_for_missing_file(self, tmp_path):
        from backend.app.utils.threemf_tools import extract_total_layers_from_3mf

        assert extract_total_layers_from_3mf(tmp_path / "nope.3mf") is None
