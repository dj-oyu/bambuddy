import json

import pytest
from fastapi import Response

from backend.app.api.routes.bmcu_link import RotateProvisioningKey, provisioning, rotate_provisioning_key
from backend.app.services.bmcu_binary.provisioning import (
    generate_device_key,
    key_file_path,
    load_key_file,
    save_key_file,
    validate_device_id,
)
from backend.app.services.bmcu_binary.server import BinaryTransportServer, binary_transport_server


def test_provisioning_key_file_is_private_and_round_trips(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    keys = {"pico-bmcu-bridge": generate_device_key()}
    save_key_file(keys)
    assert load_key_file() == keys
    assert key_file_path().stat().st_mode & 0o777 == 0o600
    assert json.loads(key_file_path().read_text()) == {"pico-bmcu-bridge": keys["pico-bmcu-bridge"].hex()}


@pytest.mark.parametrize("device_id", ["", "with space", "\ninvalid", "x" * 64])
def test_invalid_device_ids_are_rejected(device_id: str) -> None:
    with pytest.raises(ValueError):
        validate_device_id(device_id)


@pytest.mark.asyncio
async def test_rotation_updates_key_and_disconnects_old_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    server = BinaryTransportServer()
    old_key = generate_device_key()
    server._keys = {"pico-bmcu-bridge": old_key}

    class Session:
        closed = False

        async def close(self):
            self.closed = True

    session = Session()
    server.registry._sessions["pico-bmcu-bridge"] = session
    new_key = generate_device_key()
    await server.set_device_key("pico-bmcu-bridge", new_key)

    assert server.provisioning_keys()["pico-bmcu-bridge"] == new_key
    assert load_key_file()["pico-bmcu-bridge"] == new_key
    assert session.closed is True


@pytest.mark.asyncio
async def test_provisioning_api_never_allows_cache(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    previous = binary_transport_server._keys
    binary_transport_server._keys = {"pico-bmcu-bridge": b"k" * 32}
    try:
        response = Response()
        payload = await provisioning(response, None)
        assert response.headers["cache-control"] == "no-store"
        assert payload["devices"][0]["key_hex"] == (b"k" * 32).hex()

        async def set_key(device_id, key):
            binary_transport_server._keys[device_id] = key

        monkeypatch.setattr(binary_transport_server, "set_device_key", set_key)
        rotated_response = Response()
        rotated = await rotate_provisioning_key(
            RotateProvisioningKey(device_id="pico-bmcu-bridge"), rotated_response, None
        )
        assert rotated_response.headers["cache-control"] == "no-store"
        assert len(rotated["key_hex"]) == 64
        assert rotated["key_hex"] != (b"k" * 32).hex()
    finally:
        binary_transport_server._keys = previous
