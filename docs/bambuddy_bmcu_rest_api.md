# Bambuddy REST API for BMCU devices (as built)

## Which API this is

Three separate HTTP surfaces exist around a BMCU bridge. They are easily
confused because all three attract the phrase "BMCU monitor". This file
documents only the first two.

| Surface | Served by | Consumed by | Format | Documented in |
|---|---|---|---|---|
| `/api/v1/bmcu-monitors/*` | Bambuddy | Bambuddy frontend (`BMCULinkPage`) | JSON | this file, §1 |
| `/api/v1/bmcu-link/*` | Bambuddy | Bambuddy frontend (Settings, `BMCULinkSettings`) | JSON | this file, §2 |
| `/api/*.bin` | the Pico bridge itself | the Pico's own browser UI | BMB1 / BSNP / BCAP binary | `BMCU_BINARY_TRANSPORT_V1.md` §12, `bmcu_wire_layout.json` |

Bambuddy never calls the Pico's local `.bin` endpoints. Everything Bambuddy
knows about a bridge arrives over the persistent BMB1 TCP session
(`BMCU_BINARY_TRANSPORT_V1.md` §4–§8) and is persisted first; the two JSON APIs
below are reads of that persistence plus live session state. Conversely, nothing
in this file is part of the contract shared with the BMCU repository — these
routes may change without touching the wire.

"BMCU Monitor" as a noun in Bambuddy means the managed device row representing
one Pico bridge. It does not mean the Pico's local diagnostic UI, even though
that UI's responses carry `Content-Type: application/vnd.bmcu-monitor.v1`.

Source of truth for this file is the running service's OpenAPI document
(`GET /openapi.json`, verified against Bambuddy 1.2.5.2) plus
`backend/app/api/routes/bmcu_monitors.py` and `bmcu_link.py`. Regenerate the
tables from OpenAPI rather than from memory when the routes change.
`BMCU_BINARY_TRANSPORT_IMPLEMENTATION_PLAN.md` §8 holds the *planned* management
surface; this file supersedes it for what actually shipped.

## Authentication

Both routers use `RequirePermissionIfAuthEnabled`: when authentication is
disabled the routes are open, which is the current deployment's state. When it
is enabled, a request carries either a session bearer token or an API key, in
the `X-API-Key` header or as a `Bearer bb_…` token. The frontend clients send
the session token as `Authorization: Bearer`.

| Permission | Routes |
|---|---|
| `inventory:read` | every GET in both routers |
| `printers:control` | `POST /bmcu-monitors/{device_id}/control` |
| `settings:update` | `GET /bmcu-link/provisioning`, `POST /bmcu-link/provisioning/rotate` |

## 1. `/api/v1/bmcu-monitors` — monitor device views

Backs the BMCU Link page: loader state, timeline, hardware metrics, device log,
and the guarded soft reset.

| Method | Path | Query | Response |
|---|---|---|---|
| GET | `/api/v1/bmcu-monitors` | — | `MonitorSummary[]` |
| GET | `/api/v1/bmcu-monitors/{device_id}` | — | `MonitorDetail` |
| GET | `/api/v1/bmcu-monitors/{device_id}/timeline` | `from`, `to` (ISO datetime), `limit` (1–5000, default 1000) | `TimelineResponse` |
| GET | `/api/v1/bmcu-monitors/{device_id}/metrics` | `from`, `to` (ISO datetime), `limit` (1–5000, default 500) | `MetricPoint[]` |
| GET | `/api/v1/bmcu-monitors/{device_id}/logs` | `severity` (0–5), `component`, `limit` (1–1000, default 200) | untyped JSON array |
| POST | `/api/v1/bmcu-monitors/{device_id}/control` | body `ControlRequest` | `{"command_sequence": "<u64 decimal>"}` |

An unknown `device_id` is 404. `POST /control` is 409 while the monitor is
disconnected, and 422 for a bad `arguments_hex`, arguments over 128 bytes, or a
`link_index` the device never announced.

```text
MonitorSummary   deviceId, displayName, firmware, health, lastSeenAt, bootId,
                 linkCount, onlineLinks, ackSequence, replayPending, anomalyCount
MonitorDetail    MonitorSummary + firstSeenAt, links[LinkSnapshot]
LinkSnapshot     linkIndex, linkId, state, currentSlot, activeMask, motion,
                 pullPercent, pressure, faultCount, lastSeenAt
TimelineResponse points[TimelinePointResponse], from, to, downsampled
TimelinePoint    id, at, linkIndex, slot, pullPercent, pressure, motion, kind,
                 label, severity, source, anomaly, missingData
MetricPoint      at + 23 nullable diagnostic fields (heap, temperature, loop gap
                 avg/p95/p99, transport encode/send, GC, UART, Wi-Fi, ACK age,
                 journal, replay), decoded from PICO_DIAGNOSTIC TLV tags
ControlRequest   link_index (0–1), ttl_ms (1–5000, default 5000),
                 command (1 only: guarded BMCU soft reset), arguments_hex
```

Log rows are `transport_sequence`, `recorded_at`, `uptime_ms`, `severity`,
`component`, `message`, `detail_hex`. They have no response model, so OpenAPI
describes them as an untyped array.

Behavior worth knowing:

- **Both bounds present is a window request.** `/timeline` and `/metrics` thin
  the window down to `limit` rows instead of truncating it, so the answer spans
  the whole range and always includes the newest row. Sampling is anchored at
  the newest row and, for `/timeline`, is computed per BMCU kind with a max-min
  fair budget: a kind that fits its share is returned whole, and only busy kinds
  are strided. Without that split the EVENT flood — about 55,000 frames against
  847 STATUS frames over a 48 h window on the live bridge — would sample the
  loader state away. `downsampled` reports that thinning happened.
- **One bound, or none, is not a window request.** `/timeline` with a single
  bound returns the first rows after it (ascending, for a paginating caller);
  with no bound at all both routes return the newest `limit` rows. Neither is
  thinned.
- `/timeline` only reads BMCU frame kinds this decoder models (`SEMANTIC_KINDS`
  = HELLO, STATUS, EVENT, FULL_STATUS_RECORD). Other kinds are stored for later
  decoder correction and would render nothing.
- `/metrics` and `/logs` are newest-first. `/logs` treats `severity` as a floor
  (`>=`) and `component` as an exact match truncated to 40 characters.
- `severity` on timeline points is a string bucket derived from the numeric wire
  severity: `>=5` critical, `>=4` error, `>=3` warning, otherwise info.
  `anomaly` is true for warning and above.
- `LinkSnapshot`'s loader values come from the newest of two sources: a STATUS
  frame, or the GLOBAL record of a FULL_STATUS snapshot, which carries the same
  fields (`BMCU_LINK_PROTOCOL_ALPHA3.md` §6.2 in the BMCU repository). A bridge
  that emits no STATUS at all still snapshots, so the second source is what
  keeps the view alive. `statusAgeS` dates whichever one was used;
  `BAMBUDDY_BMCU_FULL_STATUS_STATE=0` disables the reconstruction.

## 2. `/api/v1/bmcu-link` — settings and provisioning

Backs the Settings panel. `bmcu_link.py` describes itself as "settings
compatibility reads backed exclusively by BMB1 persistence": the shapes predate
the binary transport and are kept for the existing Settings UI.

| Method | Path | Query / body | Response |
|---|---|---|---|
| GET | `/api/v1/bmcu-link/connection-info` | — | `auth_enabled`, `telemetry_scope` (`BMB1-HMAC`), `port`, `endpoints[{ip, tcp_url}]` |
| GET | `/api/v1/bmcu-link/provisioning` | — | `enabled`, `port`, `endpoints`, `devices[{device_id, key_hex}]` |
| POST | `/api/v1/bmcu-link/provisioning/rotate` | `{device_id}` (1–63 chars) | `{device_id, key_hex, rotated}` |
| GET | `/api/v1/bmcu-link/devices` | — | `{enabled, devices[Device]}` |
| GET | `/api/v1/bmcu-link/devices/{device_id}` | — | `Device` (404 when unknown) |
| GET | `/api/v1/bmcu-link/devices/{device_id}/events` | `kind`, `limit` (1–500, default 50), `offset` | legacy event rows |
| GET | `/api/v1/bmcu-link/devices/{device_id}/transactions` | — | `[]` |
| GET | `/api/v1/bmcu-link/enums` | — | `{"registry_version": 1}` |

`Device` carries `device_id`, `name`, `firmware`, `protocol_min`/`protocol_max`,
`capabilities`, `mode` (`production_monitor`), `link_state`,
`pico_boot_session`, `bmcu_boot_session`, `last_seen_at`, `first_seen_at`,
`last_status`, `envelope_count`, `dropped_count`, `created_at`, and
`control_key_set_at`. `bmcu_boot_session` counts observed BMCU HELLOs — no BMCU
boot id exists on the wire.

The provisioning routes return device keys in cleartext (`key_hex`) and set
`Cache-Control: no-store`. With `POST /control` in §1 they are the only writes
in either router.

`last_status` is the shared per-link snapshot (`state_view.status_snapshot`), so
it exposes both masks under their wire names — `inserted_mask` (loader hardware
presence) and `online_mask` (filament) — unlike §1's `LinkSnapshot`, which
exposes only `activeMask`.

## Conventions

- Wire `u64` values keep the text encoding of transport spec §13:
  `ackSequence`, `transport_sequence`, `uptime_ms`, and `command_sequence` are
  zero-padded 20-digit decimal strings; `bootId` / `pico_boot_session` are
  16-digit lowercase hex. They are strings on purpose — do not parse them into
  JavaScript numbers.
- Timestamps are UTC (the service runs `TZ=Asia/Shanghai`, the database stores
  UTC).
- `activeMask` (§1) is `online_mask`, i.e. **filament** detection. The hardware
  channel mask (`inserted_mask`) is not exposed by the monitors router at all.
  See `BMCUStatus` in `backend/app/services/bmcu_binary/bmcu_decoder.py` and the
  reading notes in `bmcu_wire_layout.json`.

## Known gaps

As-built facts, not intended behavior, so a caller is not misled by a field that
is currently a placeholder.

- **`/bmcu-monitors/.../timeline` ignores `resolution` and `link`.** The
  frontend client sends both. There is no per-link filter, and thinning is by
  row stride rather than by a time resolution, so the gap between returned
  samples is not the requested interval.
- **A wide window is a scan.** Thinning has to count and number every row in the
  window; a 24 h timeline request takes about 2.4 s on this hardware, against
  0.9 s for 1 h. The page refetches every 10 s, so a wider default range would
  need a coarser refetch or a pre-aggregated table.
- **`anomalyCount` is always 0** in `MonitorSummary` and `MonitorDetail`.
- **`health` is only `online` or `offline`**, derived from whether a session is
  currently registered. The spec's warning/critical aggregation
  (`BMCU_BINARY_TRANSPORT_V1.md` §12) is not computed. The frontend type also
  allows `stale`, `incompatible`, `unknown`.
- **`onlineLinks` equals `linkCount` whenever the device is connected**; it is
  not a per-link liveness count. Per-link staleness is only in
  `LinkSnapshot.state`, which goes stale after 15 s without a STATUS.
- **`/bmcu-link/enums` returns only `registry_version`.** The enum tables in
  `bmcu_binary_registry.json` are not served, so the Settings UI falls back to
  the label tables hardcoded in `BMCULinkSettings.tsx`.
- **`/bmcu-link/devices/{id}/transactions` always returns `[]`**, and `/events`
  returns `PICO_LOG` rows dressed in the legacy envelope shape
  (`kind: "pico_log"`, `uart_sequence: 0`, `bmcu_boot_session: 0`).
- The plan's `/bmcu-monitors/{id}/diagnostics` and `/history` routes were never
  added; their content is served by `/metrics` and `/timeline`. `POST /control`
  is not in the plan but shipped.
