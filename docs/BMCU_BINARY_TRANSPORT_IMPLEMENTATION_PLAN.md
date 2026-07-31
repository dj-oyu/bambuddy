# Bambuddy implementation plan: BMCU Binary Transport v1

Status: implementation-ready  
Branch: `feature/bmcu-binary-transport`  
Normative contract: `docs/BMCU_BINARY_TRANSPORT_V1.md`

## 1. Scope and outcome

Bambuddy will replace the Pico JSON WebSocket/NDJSON ingest path with an
authenticated persistent binary TCP service. It will durably ingest raw BMCU
frames, return cumulative binary ACKs, expose decoded loader history and
anomalies, store Pico diagnostics and device logs, and present each Pico as a
managed BMCU Monitor device.

This plan is Bambuddy-specific. The matching Pico work is tracked in
`docs/PICO_BINARY_TRANSPORT_IMPLEMENTATION_PLAN.md` in
`BMCU-C-PJARCZAK-kaizou`.

The implementation must not translate binary input back into the former JSON
envelope or pass it through the former Pydantic envelope schema.

## 2. Delivery boundaries

The two repositories can progress independently after shared codec vectors are
locked. Their integration boundaries are:

1. BMB1 header and message codec;
2. authentication transcript and HMAC vectors;
3. embedded BMCU frame fixtures;
4. ACK and replay behavior;
5. diagnostic and log tag registries;
6. CONTROL vectors;
7. end-to-end reconnect and journal replay tests.

Every wire-format change updates both copies of the normative contract before
implementation.

## 3. Phase B0: shared fixtures and registries

Create checked-in fixtures under `backend/tests/fixtures/bmcu_binary/`:

- valid and truncated BMB1 headers;
- partial and concatenated TCP reads;
- HELLO and HMAC known-answer vectors;
- STATUS, EVENT, FULL STATUS, and unknown BMCU frame kinds;
- LINK_STATE and TRANSPORT_DROP;
- ACK with and without rejections;
- PICO_DIAGNOSTIC tag combinations;
- PICO_LOG with UTF-8, maximum lengths, and detail TLV;
- CONTROL and CONTROL_RESULT;
- recovered-boot replay records.

Add one machine-readable registry for message types, flags, diagnostic tags,
log severity, link states, drop reasons, and rejection reasons. Generate or
validate Python and TypeScript constants from this registry so UI labels cannot
drift from the backend decoder.

Exit condition: Pico and Bambuddy tests consume byte-identical fixture files.

## 4. Phase B1: bounded binary codec

Add a transport package, initially:

```text
backend/app/services/bmcu_binary/
  __init__.py
  constants.py
  framing.py
  auth.py
  messages.py
  bmcu_decoder.py
  errors.py
```

Responsibilities:

- incremental parsing across arbitrary TCP chunk boundaries;
- maximum payload enforcement before allocation;
- encode/decode for every BMB1 message;
- strict reserved-field validation on output;
- embedded BMCU length and CRC validation;
- typed decode results using lightweight dataclasses or slots;
- direct conversion from decoded BMCU fields to persistence inputs;
- preservation of raw BMCU bytes.

The parser owns a bounded receive buffer per connection. Malformed length or
authentication data closes the connection without logging secrets or
unbounded payloads.

Tests:

- every shared fixture;
- every byte split point for representative messages;
- multiple messages per read;
- oversized length without oversized allocation;
- fuzz/property tests for parser progress and bounded failure;
- unknown optional tags are skipped.

Exit condition: the codec is usable without FastAPI, a database, or JSON.

## 5. Phase B2: TCP listener and authenticated session

Add a lifecycle-managed `asyncio.start_server` service. Suggested files:

```text
backend/app/services/bmcu_binary/server.py
backend/app/services/bmcu_binary/session.py
backend/app/services/bmcu_binary/registry.py
```

Configuration:

- enabled flag;
- bind address;
- dedicated TCP port;
- maximum concurrent monitor connections;
- idle and authentication timeouts;
- per-connection input/output bounds;
- device-key provisioning source.

Integrate start/stop with the existing application lifespan. One active
session is allowed per device ID; a newly authenticated boot session replaces
an older stale socket deterministically.

Session state machine:

```text
ACCEPTED -> CHALLENGE_SENT -> AUTHENTICATED -> ONLINE -> CLOSED
```

Before authentication only HELLO is accepted. Use constant-time HMAC
comparison. Authentication failures are rate-limited and never expose whether
a device ID or key was correct.

PING/PONG and write-side backpressure are bounded. UI broadcasts never run
inside the TCP read/parser callback.

Tests:

- correct/incorrect/malformed HMAC;
- HELLO timeout;
- telemetry before authentication;
- duplicate device sessions;
- slow reader and slow writer;
- clean shutdown and application restart.

Exit condition: a fixture client can authenticate and exchange binary
PING/PONG without touching legacy BMCU routes.

## 6. Phase B3: persistence and durable ACK

Create a migration that preserves existing BMCU data while adding binary
identity and telemetry storage.

Required data model:

### Monitor device/session

- stable device ID and display name;
- firmware and capabilities;
- current boot ID;
- first/last seen;
- connection and aggregate health;
- provisioned-key metadata, never the key itself in API responses;
- last durable ACK per boot/link;
- oldest pending replay information reported by HELLO.

### Raw transport event

- device ID;
- Pico boot ID;
- transport sequence;
- link index and resolved link ID;
- received monotonic timestamp;
- Bambuddy receive timestamp;
- message/frame kind;
- raw BMCU frame bytes;
- decoded indexed fields needed by queries;
- replay and critical flags.

Enforce uniqueness on:

```text
device_id, pico_boot_id, transport_sequence
```

### Diagnostics

Store current diagnostic snapshot separately from sampled historical points.
Historical columns cover heap, temperature, loop delay, UART backlog/errors,
Wi-Fi, TCP/replay, journal, queue, and ACK age. Preserve unknown tags in a
bounded binary extras column if forward inspection is required.

### Device logs

Store log sequence, uptime, severity, component, message, bounded detail TLV,
boot ID, receive time, and recovered-crash flag. Index device/time, severity,
and component.

ACK rules:

- stage all accepted messages in one bounded transaction;
- commit;
- compute the highest contiguous persisted sequence per boot/link;
- enqueue ACK only after successful commit;
- never advance across an absent or retryable-rejected sequence;
- duplicates are successful idempotent input, not duplicate rows;
- a database error sends no success ACK.

Tests:

- rollback produces no ACK;
- duplicate replay produces one row and advances correctly;
- out-of-order sequences do not skip gaps;
- reconnect resumes the correct watermark;
- logs and diagnostics remain bounded.

Exit condition: binary input is durable and replay-safe.

## 7. Phase B4: BMCU frame decoding and timeline analysis

Port the BMCU wire decoder needed by Bambuddy from the shared protocol
documents and fixtures. Do not import MicroPython application modules.

Decode:

- HELLO/session changes;
- STATUS;
- EVENT and state_change;
- FULL STATUS records;
- current slot, masks, motion, pull percentage, pressure, faults, and counters;
- unknown kinds as retained raw records.

Update existing BMCU device state from decoded fields. Feed loader analysis
with:

- motion intervals per slot;
- pull-percentage series;
- pressure series;
- slot/mask changes;
- BMCU-reported warning/error/critical events;
- Bambuddy-detected anomaly windows;
- stale/offline and missing-data intervals;
- Pico and BMCU reboot boundaries.

BMCU-reported and Bambuddy-derived anomalies remain visibly distinct.

Add range-based query services rather than relying on the existing offset-only
50-event endpoint. Downsample STATUS for long ranges without dropping EVENT
markers or transition boundaries.

Tests:

- known frame fixtures decode identically to Pico UI expectations;
- transition intervals close at the next event;
- link gaps create missing-data regions, not interpolation;
- anomaly clusters preserve their source and severity.

Exit condition: the graphical loader timeline can be driven entirely from the
binary persistence path.

## 8. Phase B5: management APIs

Add authenticated REST APIs for the Bambuddy frontend. These APIs may use JSON
because they run on the server, not on the Pico hot path.

Suggested surface:

```text
GET /api/v1/bmcu-monitors
GET /api/v1/bmcu-monitors/{device_id}
GET /api/v1/bmcu-monitors/{device_id}/timeline
GET /api/v1/bmcu-monitors/{device_id}/diagnostics
GET /api/v1/bmcu-monitors/{device_id}/metrics
GET /api/v1/bmcu-monitors/{device_id}/logs
GET /api/v1/bmcu-monitors/{device_id}/history
```

Add bounded `from`, `to`, `resolution`, `severity`, `component`, `link`, and
slot filters as appropriate. Apply the existing settings/BMCU permissions
until a dedicated permission is introduced.

Keep provisioning and sensitive device-key actions in Settings. Never return a
device key after creation.

Exit condition: all frontend views have range-precise APIs without reading the
Pico local UI endpoints.

## 9. Phase B6: frontend device management

Add `/bmcu-link` and a sidebar entry separate from Settings.

Suggested frontend structure:

```text
frontend/src/pages/BMCULinkPage.tsx
frontend/src/components/bmcu/
  MonitorCard.tsx
  LoaderTimeline.tsx
  SlotStateGrid.tsx
  HealthSummary.tsx
  HardwareMetrics.tsx
  CommunicationsPanel.tsx
  DeviceLog.tsx
  LocalHistoryPanel.tsx
```

Overview:

- Monitor cards similar to printer cards;
- online/stale/offline and aggregate health;
- firmware, uptime, last seen, heap, temperature, RSSI, queue, journal, ACK;
- per-link state;
- navigation to the device detail.

Device detail tabs:

1. Overview: slots, loader timeline, integrated anomalies.
2. Hardware: heap, temperature, loop delay and GC history.
3. Communications: UART, sequence/CRC/frame errors, Wi-Fi, TCP, ACK/replay.
4. Device log: severity/component/time filters and recovered crash.
5. Local history: retained range, capacity, segments, pending replay and loss.

The loader graph uses one shared time axis for motion, pull percentage,
pressure, anomaly markers, and link/data-loss regions. Clicking an anomaly
opens its correlated event context.

Settings retains:

- listener status and address;
- device-key provisioning;
- detailed raw/event logs;
- protocol/session metadata;
- diagnostic administration.

Tests:

- routing, permissions, sidebar ordering and hidden-item behavior;
- empty/disabled/offline/loading states;
- range and filter behavior;
- anomaly markers and missing-data intervals;
- accessible non-color-only health indicators;
- responsive compact/sidebar layouts.

Exit condition: normal operation does not require the Settings event table.

## 10. Phase B7: CONTROL migration

Move the existing CONTROL gateway to binary session-derived HMAC messages.
Preserve command ownership, TTL, replay defense, audit logging, and the
existing limited safe command surface.

Do not expand motor, slot, or filament control as part of transport migration.
Use shared known-answer vectors for signing and rejection.

Exit condition: every existing permitted CONTROL operation succeeds through
BMB1 and every legacy JSON CONTROL path is unused.

## 11. Phase B8: cutover and legacy removal

After binary end-to-end and recovery tests pass:

- remove or disable the JSON WebSocket ingest route;
- remove HTTP/HTTPS NDJSON ingest;
- remove legacy JSON envelope construction assumptions;
- remove old ACK adaptation code;
- stop starting the former watchdog/client state tied to JSON sessions;
- migrate Settings copy from JSON bridge URLs to TCP provisioning;
- retain old database rows read-only if required for historical UI;
- update operational documentation and environment examples.

There is no dual-protocol production period. Development fixtures may retain
legacy samples only for migration regression tests.

Exit condition: no enabled production entry point accepts Pico telemetry JSON.

## 12. Integration and release sequence

Recommended merge sequence:

1. shared fixtures and Bambuddy codec;
2. TCP listener behind a disabled feature flag;
3. migrations and durable ACK;
4. decoder and management APIs;
5. Pico connects to the disabled-by-default listener in integration tests;
6. frontend management page;
7. binary CONTROL;
8. enable binary transport and remove legacy paths;
9. soak/reconnect/power-loss test report;
10. production documentation.

The feature branch is not release-ready until the matching Pico branch passes
the shared end-to-end suite.

## 13. Bambuddy definition of done

- The binary TCP service is bounded, authenticated, lifecycle-managed, and
  observable.
- ACKs prove durable commit and never cross sequence gaps.
- Replays are idempotent across reconnect and Bambuddy restart.
- Raw BMCU bytes and decoded indexed fields are retained.
- Loader and anomaly graphs use binary-ingested data.
- Pico hardware, communication, logs, crash recovery, and local-history
  status are manageable from the sidebar page.
- Settings contains provisioning and diagnostic administration only.
- Secrets never appear in logs or API responses.
- The shared malformed-input, authentication, replay, CONTROL, and integration
  suites pass.
- Legacy Pico JSON WebSocket and NDJSON ingestion are removed.

