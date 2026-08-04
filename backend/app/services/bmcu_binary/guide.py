"""Reading notes and thresholds for the BMCU Monitor API.

`/openapi.json` says what shape a response has and what each field means. It
cannot say "this counter pinned at its maximum is steady state, not a stall" --
that is knowledge about how to read the numbers together, and it is exactly the
knowledge a reader had to go to the source for. Issue #3 asked for it to be
served; the bridge does the same thing with `reading_notes` and `thresholds` in
its own `/api/schema.json`.

Two rules for what belongs here:

- only what a caller cannot derive from the schema. Units and baselines go in
  `Field(description=...)`, not here;
- only what is true of *this* API. The bridge's own counters are documented by
  the bridge; duplicating them would create a second copy to drift.

Everything below is a statement someone got wrong at least once.
"""

from __future__ import annotations

READING_NOTES: tuple[dict[str, str | None], ...] = (
    {
        "field": "links[].state",
        "note": (
            "`no_data` and `stale` are different failures. `no_data` means no STATUS has ever been "
            "decoded for the link, so every loader field is null. `stale` means one was decoded and "
            "the bridge has sent none since -- that is a bridge that stopped talking, and it is the "
            "one worth an alert."
        ),
    },
    {
        "field": "links[].statusAgeS",
        "note": (
            "This, not lastSeenAt, is whether the loader view is current. The device-level lastSeenAt "
            "tracks the transport, which keeps advancing on EVENT and diagnostic frames while STATUS "
            "is starved; a link showed a day-old slot as live for exactly that reason."
        ),
    },
    {
        "field": "links[].activeMask",
        "note": (
            "Filament presence (the microswitch), not the hardware channel mask. A loader that is "
            "installed but empty reports its bit clear here. The hardware mask is not exposed by this "
            "router at all."
        ),
    },
    {
        "field": "links[].faultCount",
        "note": (
            "Cumulative since the BMCU booted, so the absolute value says almost nothing. Sample "
            "twice and report the delta with the elapsed time. A counter that does not advance is as "
            "informative as one that does."
        ),
    },
    {
        "field": "replayPending",
        "note": (
            "Non-zero is normal: it is the backlog the bridge has not yet had acknowledged. Stalled "
            "delivery is a value that does not fall between two reads while ackSequence also fails to "
            "advance. A single sample cannot distinguish the two."
        ),
    },
    {
        "field": "ackSequence",
        "note": (
            "A zero-padded 20-digit decimal string, and it must stay one. It is a u64; parsing it "
            "into a JavaScript number loses the low bits."
        ),
    },
    {
        "field": "timeline.points[].at",
        "note": (
            "Server receive time, not the BMCU hardware tick. hw_tick32 runs at 18 MHz and wraps "
            "about every 238 s, so it cannot order points across a window."
        ),
    },
    {
        "field": None,
        "note": (
            "Timestamps are UTC. The service runs TZ=Asia/Shanghai and its logs are in CST, which is "
            "eight hours ahead of everything this API returns."
        ),
    },
    {
        "field": None,
        "note": (
            "Null is always 'not known' and never 'known to be zero'. Any field that could report a "
            "real zero reports null instead when there is no reading behind it."
        ),
    },
)

THRESHOLDS: tuple[dict[str, object], ...] = (
    {
        "metric": "links[].statusAgeS",
        "warn_above": 20.0,
        "unit": "seconds",
        "note": (
            "The feed-stall watcher ignores any status older than 20 s, so above this the print "
            "safety watch is not armed. It fails safe -- no data means no alarm -- but it is also "
            "protecting nothing."
        ),
    },
    {
        "metric": "links[].pullPercent",
        "warn_below": 40,
        "unit": "percent",
        "note": (
            "50 is neutral. Below 40 during pressure-controlled use is what the BMCU itself latches "
            "as `low`; read links[].state and the bridge's own channel flags before concluding."
        ),
    },
    {
        "metric": "replayPending",
        "warn_above": 0,
        "note": "Only meaningful across two samples; see the reading note. A single non-zero read is not a fault.",
    },
)
