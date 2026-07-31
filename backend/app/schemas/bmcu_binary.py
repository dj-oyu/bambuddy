from datetime import datetime

from pydantic import BaseModel, Field


class MonitorSummary(BaseModel):
    deviceId: str
    displayName: str
    firmware: str
    health: str
    lastSeenAt: datetime | None
    bootId: str | None
    linkCount: int
    onlineLinks: int
    ackSequence: str
    replayPending: int
    anomalyCount: int


class LinkSnapshot(BaseModel):
    linkIndex: int
    linkId: str
    state: str
    currentSlot: int | None
    activeMask: int
    motion: str | None
    pullPercent: int | None
    pressure: int | None
    faultCount: int
    lastSeenAt: datetime | None


class MonitorDetail(MonitorSummary):
    firstSeenAt: datetime | None
    links: list[LinkSnapshot]


class TimelinePointResponse(BaseModel):
    id: str
    at: datetime
    linkIndex: int | None
    slot: int | None
    pullPercent: int | None = None
    pressure: int | None = None
    motion: str | None = None
    kind: str
    label: str
    severity: str
    source: str
    anomaly: bool
    missingData: bool


class TimelineResponse(BaseModel):
    points: list[TimelinePointResponse]
    from_: datetime = Field(alias="from")
    to: datetime
    downsampled: bool


class MetricPoint(BaseModel):
    at: datetime
    heapFreeBytes: int | None = None
    temperatureC: float | None = None
    loopDelayUs: int | None = None
    uartBacklog: int | None = None
    uartErrors: int | None = None
    wifiRssiDbm: int | None = None
    replayPending: int | None = None
    journalBytes: int | None = None
    ackAgeMs: int | None = None
    loopGapAvgUs: int | None = None
    loopGapP95Us: int | None = None
    loopGapP99Us: int | None = None
    transportEncodeAvgUs: int | None = None
    transportSendAvgUs: int | None = None
    transportSendMaxUs: int | None = None
    gcLastUs: int | None = None
    gcMaxUs: int | None = None
    uartDrainBytes: int | None = None
    uartOverflowTotal: int | None = None
    uartBacklogMax: int | None = None
    uartServiceDelayUs: int | None = None
    uartCrcErrorsTotal: int | None = None
    uartSequenceGapsTotal: int | None = None


class ControlRequest(BaseModel):
    link_index: int = Field(ge=0, le=1)
    ttl_ms: int = Field(default=5000, ge=1, le=5000)
    command: int = Field(default=1, ge=1, le=1)
    arguments_hex: str = ""
