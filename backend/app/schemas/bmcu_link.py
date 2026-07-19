"""Pydantic schemas for the BMCU Link adapter (schema "bmcu.management.v2")."""

import logging
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

EXPECTED_SCHEMA = "bmcu.management.v2"


class BMCULinkFrame(BaseModel):
    kind: str
    kind_id: int | None = None
    protocol: int | None = None


class BMCULinkLink(BaseModel):
    """Link identity per issue #2 contract: `transport_sequence` (u64,
    monotonic per Pico boot, unique per envelope) is the dedup/ordering key;
    the BMCU UART sequence (u16, wraps, shared across full-status records) is
    kept as `bmcu_sequence` for gap diagnostics only. Older spellings
    `sequence`/`uart_sequence` are accepted for the BMCU value."""

    state: str
    transport_sequence: int | None = None
    uart_sequence: int | None = Field(
        default=None, validation_alias=AliasChoices("bmcu_sequence", "sequence", "uart_sequence")
    )
    pico_boot_session: str
    # Optional since the Phase 5 interop spec: transport-link envelopes
    # (link.id == "transport", e.g. the transport HELLO) describe the Pico
    # itself and may carry no BMCU session. Rejecting them would quarantine
    # the HELLO on the bridge and stall its sequencing.
    bmcu_boot_session: int | None = None
    id: str = "default"
    queue_depth: int | None = None

    @model_validator(mode="after")
    def _require_some_sequence(self):
        if self.transport_sequence is None and self.uart_sequence is None:
            raise ValueError("link requires transport_sequence or bmcu_sequence")
        return self

    @property
    def dedup_sequence(self) -> int:
        """transport_sequence when present (production), else the legacy
        BMCU sequence (commissioning poller / pre-contract bridges)."""
        return self.transport_sequence if self.transport_sequence is not None else self.uart_sequence


class BMCULinkEnvelope(BaseModel):
    """One pushed envelope from the Pico bridge. Unknown fields are allowed
    (forward compatibility with newer bridge firmware)."""

    model_config = {"extra": "allow"}

    schema_: str = Field(alias="schema")
    device_id: str
    received_at_us: int
    received_at: datetime | None = None
    registry_version: str | int | None = None
    mode: str | None = None  # production_monitor | bench_stub
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


class BMCULinkPersistedKey(BaseModel):
    """Highest fully persisted dedup key for one (device, link) — the Pico
    may discard its replay buffer only up to this point (issue #2 shape)."""

    link_id: str
    pico_boot_session: str
    transport_sequence: int


class BMCULinkRejected(BaseModel):
    """One rejected envelope in a partial-accept batch (issue #2 contract).
    Codes are append-only stable strings; retryable=False means the bridge
    must quarantine the record (count it as dropped) and move on."""

    index: int
    transport_sequence: int | None = None
    code: str  # validation_error | batch_too_large | device_cap | link_cap | internal
    retryable: bool


class BMCULinkIngestResponse(BaseModel):
    accepted: int
    deduplicated: int
    rejected: list[BMCULinkRejected] = Field(default_factory=list)
    # Per-link persistence watermark; lags accepted counts because rows are
    # batch-flushed. Absent until the first flush lands.
    persisted: list[BMCULinkPersistedKey] = Field(default_factory=list)


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
    # CONTROL provisioning status only — the key itself is never serialized.
    control_key_set_at: datetime | None = None


class BMCULinkEventResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    device_id: str
    link_id: str = "default"
    pico_boot_session: str
    bmcu_boot_session: int
    uart_sequence: int
    transport_sequence: int | None = None
    kind: str
    kind_id: int | None = None
    protocol: int | None = None
    received_at_us: int
    received_at: datetime | None = None
    server_received_at: datetime
    transaction_id: str | None = None
    data: str
