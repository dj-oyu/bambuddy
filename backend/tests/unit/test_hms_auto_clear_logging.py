"""Auto-clear must log the HMS error's human-readable meaning BEFORE clearing.

Background (2026-07-13 runout incident): 0300_800B ("cutter is stuck / check
filament sensor cable") was auto-cleared in a loop all night; nobody read the
error text until 9 hours later because clearing removed it from every surface.
The WARNING at clear time keeps the hint in journalctl.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.app import main as app_main


def _error(short_code: str) -> SimpleNamespace:
    attr_hi, code = short_code.split("_")
    return SimpleNamespace(attr=int(attr_hi, 16) << 16, code=int(code, 16))


def _client(state: str = "IDLE") -> MagicMock:
    client = MagicMock()
    client.state = SimpleNamespace(state=state)
    client.clear_hms_errors.return_value = False  # no requeue side effects
    return client


class TestAutoClearDescriptionLogging:
    def test_clear_logs_description_warning(self, caplog):
        code = "0300_800B"
        with (
            patch.object(app_main, "_HMS_AUTO_CLEAR_CODES", {code}),
            patch.dict(app_main._hms_auto_clear_attempts, {}, clear=True),
            patch(
                "backend.app.services.printer_manager.printer_manager.get_client",
                return_value=_client(),
            ),
        ):
            with caplog.at_level("WARNING"):
                app_main._maybe_auto_clear_hms(1, [_error(code)])
        warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any(code in m and "cutter" in m.lower() for m in warnings), warnings

    def test_unknown_code_logs_placeholder(self, caplog):
        code = "0500_409D"  # BMCU third-party code, absent from the HMS DB
        with (
            patch.object(app_main, "_HMS_AUTO_CLEAR_CODES", {code}),
            patch.dict(app_main._hms_auto_clear_attempts, {}, clear=True),
            patch(
                "backend.app.services.printer_manager.printer_manager.get_client",
                return_value=_client(),
            ),
        ):
            with caplog.at_level("WARNING"):
                app_main._maybe_auto_clear_hms(1, [_error(code)])
        warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any(code in m for m in warnings), warnings
