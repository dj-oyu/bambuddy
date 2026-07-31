"""Persistent provisioning keys for BMB1 devices."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from backend.app.core.paths import resolve_data_dir

KEY_FILE_NAME = ".bmcu_binary_keys.json"


def key_file_path() -> Path:
    return resolve_data_dir() / KEY_FILE_NAME


def validate_device_id(device_id: str) -> str:
    value = device_id.strip()
    encoded = value.encode("utf-8")
    if (
        value != device_id
        or not value
        or len(encoded) > 63
        or any(ord(char) < 0x21 for char in value)
    ):
        raise ValueError("device_id must contain 1-63 printable non-space bytes")
    return value


def _validate_mapping(raw: object) -> dict[str, bytes]:
    if not isinstance(raw, dict):
        raise ValueError("key mapping must be an object")
    result: dict[str, bytes] = {}
    for device_id, encoded in raw.items():
        if not isinstance(device_id, str) or not isinstance(encoded, str):
            raise ValueError("key mapping entries must be strings")
        device_id = validate_device_id(device_id)
        key = bytes.fromhex(encoded)
        if len(key) != 32:
            raise ValueError("device keys must be 256 bits")
        result[device_id] = key
    return result


def load_key_file() -> dict[str, bytes]:
    path = key_file_path()
    if not path.exists():
        return {}
    return _validate_mapping(json.loads(path.read_text(encoding="utf-8")))


def save_key_file(keys: dict[str, bytes]) -> None:
    path = key_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {device_id: key.hex() for device_id, key in sorted(keys.items())},
        separators=(",", ":"),
    ).encode()
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def generate_device_key() -> bytes:
    return secrets.token_bytes(32)
