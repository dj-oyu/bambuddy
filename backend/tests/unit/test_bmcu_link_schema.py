"""BMCU Link envelope schema validation tests."""

import pydantic
import pytest

from backend.app.schemas.bmcu_link import BMCULinkEnvelope


def make_envelope(**overrides):
    env = {
        "schema": "bmcu.management.v2",
        "device_id": "bmcu-bridge-1",
        "received_at_us": 123456789,
        "link": {
            "state": "online",
            "uart_sequence": 1234,
            "pico_boot_session": "abc123",
            "bmcu_boot_session": 3,
        },
        "frame": {"kind": "status", "kind_id": 2, "protocol": 2},
        "data": {"slots": [1, 2, 3, 4]},
    }
    env.update(overrides)
    return env


def test_valid_envelope_parses():
    env = BMCULinkEnvelope.model_validate(make_envelope())
    assert env.schema_ == "bmcu.management.v2"
    assert env.device_id == "bmcu-bridge-1"
    assert env.link.uart_sequence == 1234
    assert env.frame.kind == "status"
    assert env.data == {"slots": [1, 2, 3, 4]}


def test_missing_received_at_us_fails():
    payload = make_envelope()
    del payload["received_at_us"]
    with pytest.raises(pydantic.ValidationError):
        BMCULinkEnvelope.model_validate(payload)


def test_unknown_kind_accepted():
    env = BMCULinkEnvelope.model_validate(make_envelope(frame={"kind": "future_frame_kind"}))
    assert env.frame.kind == "future_frame_kind"
    assert env.frame.kind_id is None


def test_extra_fields_allowed():
    payload = make_envelope()
    payload["some_future_field"] = {"nested": True}
    env = BMCULinkEnvelope.model_validate(payload)
    assert env.device_id == "bmcu-bridge-1"


def test_schema_mismatch_warns_not_rejects(caplog):
    env = BMCULinkEnvelope.model_validate(make_envelope(**{"schema": "bmcu.management.v3"}))
    assert env.schema_ == "bmcu.management.v3"


def test_array_parse():
    from backend.app.api.routes.bmcu_link import _parse_envelopes_partial

    envs, rejected = _parse_envelopes_partial([make_envelope(), make_envelope(device_id="other")])
    assert len(envs) == 2 and not rejected
    assert envs[1][1].device_id == "other"
    assert [i for i, _ in envs] == [0, 1]

    single, rejected = _parse_envelopes_partial(make_envelope())
    assert len(single) == 1 and not rejected
