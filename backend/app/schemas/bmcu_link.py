"""Pydantic schemas for the BMCU Link adapter (schema "bmcu.management.v2")."""

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

EXPECTED_SCHEMA = "bmcu.management.v2"


class BMCULinkFrame(BaseModel):
    kind: str
    kind_id: int | None = None
    protocol: int | None = None


class BMCULinkLink(BaseModel):
    state: str
    uart_sequence: int
    pico_boot_session: str
    bmcu_boot_session: int


class BMCULinkEnvelope(BaseModel):
    """One pushed envelope from the Pico bridge. Unknown fields are allowed
    (forward compatibility with newer bridge firmware)."""

    model_config = {"extra": "allow"}

    schema_: str = Field(alias="schema")
    device_id: str
    received_at_us: int
    received_at: datetime | None = None
    link: BMCULinkLink
    frame: BMCULinkFrame
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("schema_")
    @classmethod
    def _warn_on_schema_mismatch(cls, v: str) -> str:
        # Warn-not-reject: a newer bridge schema should still be ingested.
        if v != EXPECTED_SCHEMA:
            logger.warning("BMCU Link envelope with unexpected schema %r (expected %r)", v, EXPECTED_SCHEMA)
        return v


class BMCULinkHelloData(BaseModel):
    model_config = {"extra": "allow"}

    firmware: str | None = None
    protocol_min: int | None = None
    protocol_max: int | None = None
    capabilities: list[str] = Field(default_factory=list)
    mode: str | None = None  # production_monitor | bench_stub


class BMCULinkIngestResponse(BaseModel):
    accepted: int
    deduplicated: int


class BMCULinkDeviceResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    device_id: str
    name: str | None = None
    firmware: str | None = None
    protocol_min: int | None = None
    protocol_max: int | None = None
    capabilities: str | None = None
    mode: str | None = None
    link_state: str
    pico_boot_session: str | None = None
    bmcu_boot_session: int | None = None
    last_seen_at: datetime | None = None
    first_seen_at: datetime | None = None
    last_status: str | None = None
    envelope_count: int
    dropped_count: int
    created_at: datetime | None = None


class BMCULinkEventResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    device_id: str
    pico_boot_session: str
    bmcu_boot_session: int
    uart_sequence: int
    kind: str
    kind_id: int | None = None
    protocol: int | None = None
    received_at_us: int
    received_at: datetime | None = None
    server_received_at: datetime
    transaction_id: str | None = None
    data: str
