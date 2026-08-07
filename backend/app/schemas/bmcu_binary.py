"""Response models for the BMCU Monitor read API.

Two rules run through this file, both from issue #3.

**Null means "not known", never "known to be zero".** A caller reading this API
cannot open the source to find out which of the two it is looking at, so no
field may answer "no data" with a value that is also a legitimate reading. An
`activeMask` of 0 is four empty channels; a link that has never reported one
sends null.

**Every field a machine has to interpret says so in the schema.** Units,
baselines and the set of values a field can take belong in `/openapi.json`,
not in a reader's memory. `LinkState` is a Literal rather than `str` for that
reason -- the enumeration is then part of the published contract.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# What the transport knows about the device as a whole. Only these two are ever
# produced: the device either holds an open BMB1 session or it does not.
MonitorHealth = Literal["online", "offline"]

# What is known about one loader link's STATUS view.
#   online  - decoded within _STALE_AFTER_S; the loader fields are current
#   stale   - decoded, but longer ago than that; the fields are a past reading
#   no_data - never decoded for this link; every loader field below is null
# `stale` and `no_data` were both reported as "stale" before issue #3, so a
# link that had never said anything was indistinguishable from one that had
# gone quiet -- and only the second one means the bridge lost something.
LinkState = Literal["online", "stale", "no_data"]

# Timeline point categories, from services/bmcu_binary/timeline.py.
TimelineSeverity = Literal["info", "warning", "error", "critical"]
TimelineSource = Literal["bmcu", "bambuddy", "transport", "pico"]


class MonitorSummary(BaseModel):
    deviceId: str = Field(description="Stable device identifier the bridge authenticates with.")
    displayName: str
    firmware: str = Field(description="Firmware string the bridge reported in its transport HELLO.")
    health: MonitorHealth = Field(description="Whether a BMB1 session is open right now.")
    lastSeenAt: datetime | None = Field(
        description=(
            "UTC. Last traffic of any kind from the device. This tracks the transport, not the "
            "loader: it keeps advancing on EVENT and diagnostic frames while STATUS is starved, "
            "so it cannot tell you whether the loader view is current. Use LinkSnapshot.statusAgeS "
            "for that."
        )
    )
    bootId: str | None = Field(description="16-digit lowercase hex. Changes when the bridge reboots.")
    linkCount: int
    onlineLinks: int
    ackSequence: str = Field(
        description="Zero-padded 20-digit decimal string, not a number. Highest contiguously persisted "
        "transport sequence."
    )
    replayPending: int = Field(
        description="Records the bridge holds above ackSequence. Non-zero is normal while it catches "
        "up; a value that does not fall between two reads is the signal."
    )
    anomalyCount: int | None = Field(
        default=None,
        description=(
            "Not computed yet -- always null. It is null rather than 0 because 0 would assert that "
            "the device is reporting nothing wrong, which this API has no basis for."
        ),
    )


class LinkSnapshot(BaseModel):
    linkIndex: int
    linkId: str
    state: LinkState = Field(
        description="How much is known about this link's loader view. Every field below is null when this is `no_data`."
    )
    currentSlot: int | None = Field(
        description="Zero-based selected channel. Null means no channel is selected (wire value 0xFF) "
        "when state is not `no_data`, and unknown when it is."
    )
    activeMask: int | None = Field(
        description="Per-channel filament presence bitmask, bit 0 = channel 0. This is the online "
        "mask (the microswitch), not the hardware channel mask. 0 means four empty channels; unknown "
        "is null."
    )
    motion: list[int] | None = Field(
        description="Per-channel AMS motion enum, four entries, channel order. Was a stringified "
        "Python tuple before issue #3."
    )
    pullPercent: int | None = Field(
        description="Pull reading of the selected channel, percent, 50 is neutral. Null when no "
        "channel is selected, so there is nothing to report for."
    )
    pressure: int | None = Field(description="Raw pressure reading as the BMCU reports it; no unit conversion.")
    faultCount: int | None = Field(
        description="BMCU-side crc_error + frame_error, cumulative since the BMCU booted. Compare two "
        "reads: the rate is the signal, not the total."
    )
    statusAgeS: float | None = Field(
        default=None,
        description="Seconds since the values above were decoded. Null when state is `no_data`. This "
        "is the field that says whether the loader view is current.",
    )
    lastSeenAt: datetime | None = Field(
        description="UTC wall clock for the same reading statusAgeS ages. Null when state is `no_data`."
    )


class MonitorDetail(MonitorSummary):
    firstSeenAt: datetime | None
    links: list[LinkSnapshot]


class GuideNote(BaseModel):
    field: str | None = Field(description="Dotted path the note applies to; null for API-wide notes.")
    note: str


class GuideThreshold(BaseModel):
    metric: str = Field(description="Dotted path of the field being bounded.")
    warnAbove: float | None = None
    warnBelow: float | None = None
    unit: str | None = None
    note: str


class MonitorGuide(BaseModel):
    """What `/openapi.json` cannot express: how to read the numbers together.

    The bridge serves the same two sections in its own `/api/schema.json`. These
    describe bambuddy's API only -- the device's counters are documented by the
    device, and a second copy here would only drift.
    """

    readingNotes: list[GuideNote]
    thresholds: list[GuideThreshold]


class TimelinePointResponse(BaseModel):
    id: str
    at: datetime = Field(description="UTC, server receive time -- not the BMCU hardware tick.")
    linkIndex: int | None
    slot: int | None = Field(description="Zero-based channel the point belongs to; null for link-wide points.")
    pullPercent: int | None = Field(default=None, description="Set only on `pull_pct` points. Percent, 50 neutral.")
    pressure: int | None = Field(default=None, description="Set only on `pressure` points.")
    motion: int | None = Field(
        default=None,
        description="Set only on `motion` points, and on `state_change` points whose field IS motion: "
        "the AMS motion enum value for that channel. Was a stringified int before issue #3, and "
        "carried every state_change's value -- including motion_fault -- until 2026-08-07.",
    )
    field: int | None = Field(
        default=None,
        description="Set only on `state_change` points: the registry `state_field` id that moved. "
        "Without it every state change reads alike and a latching motion_fault cannot be told "
        "from a slot selection.",
    )
    fieldName: str | None = Field(
        default=None,
        description="Registry name for `field` (slot, inserted_mask, online_mask, motion, pressure, "
        "led_mode, control_error, motion_fault). Null when this build has no name for the id, which "
        "is a different answer from the point not being a state change.",
    )
    kind: str = Field(description="Point category, e.g. current_slot, motion, pull_pct, event, state_change.")
    label: str
    severity: TimelineSeverity
    source: TimelineSource
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
