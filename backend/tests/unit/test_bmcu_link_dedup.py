"""BMCU Link dedup window tests."""

from backend.app.schemas.bmcu_link import BMCULinkEnvelope
from backend.app.services.bmcu_link import BMCULinkService


def make_env(seq=1, device_id="dev1", pico="picoA", bmcu=1):
    return BMCULinkEnvelope.model_validate(
        {
            "schema": "bmcu.management.v2",
            "device_id": device_id,
            "received_at_us": 1000 + seq,
            "link": {
                "state": "online",
                "uart_sequence": seq,
                "pico_boot_session": pico,
                "bmcu_boot_session": bmcu,
            },
            "frame": {"kind": "status", "kind_id": 2, "protocol": 2},
            "data": {},
        }
    )


def test_duplicate_rejected():
    svc = BMCULinkService()
    assert svc.is_duplicate(make_env(seq=1)) is False
    assert svc.is_duplicate(make_env(seq=1)) is True
    assert svc.is_duplicate(make_env(seq=2)) is False


def test_window_eviction():
    svc = BMCULinkService()
    svc.DEDUP_WINDOW = 4
    for seq in range(1, 6):  # 5 entries, window 4 -> seq 1 evicted
        assert svc.is_duplicate(make_env(seq=seq)) is False
    assert svc.is_duplicate(make_env(seq=1)) is False  # evicted, accepted again
    assert svc.is_duplicate(make_env(seq=5)) is True  # still in window


def test_boot_session_reset():
    svc = BMCULinkService()
    assert svc.is_duplicate(make_env(seq=1, bmcu=1)) is False
    # BMCU boot alone no longer resets the window (transport_sequence /
    # pico session own ordering per issue #2); same key -> duplicate.
    assert svc.is_duplicate(make_env(seq=1, bmcu=2)) is True
    assert svc.is_duplicate(make_env(seq=1, bmcu=2)) is True
    assert svc.is_duplicate(make_env(seq=1, bmcu=1)) is True
    # A PICO boot change resets the window; sequences may repeat.
    assert svc.is_duplicate(make_env(seq=1, pico="pico-B")) is False


def test_dedup_isolated_per_device():
    svc = BMCULinkService()
    assert svc.is_duplicate(make_env(seq=1, device_id="a")) is False
    assert svc.is_duplicate(make_env(seq=1, device_id="b")) is False
