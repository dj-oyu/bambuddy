# BMCU Monitor Stream v2 receiver

Bambuddy accepts the BMS2 UDP stream described by the Pico's
[BMCU Monitor Stream v2 specification](https://github.com/dj-oyu/BMCU-C-PJARCZAK-kaizou/blob/e4bdee4e6d2530192ca513bb07b3523a32b2a80b/docs/BMCU_MONITOR_STREAM_V2.md).
The listener uses `BMCU_BINARY_HOST` and `BMCU_BINARY_PORT` (default UDP 8799).
The v1 TCP listener can run on the same numeric port during migration.

UDP telemetry has no authentication or replay. Only use it on a trusted LAN.
The device-ID allowlist prevents accidental registration; it does not authenticate
senders. Telemetry can affect Bambuddy features that act on BMCU state.

- `BAMBUDDY_BMCU_STREAM_UDP=0` disables the UDP listener (enabled by default).
- Previously registered device IDs and configured v1 device-key IDs are accepted.
  For a new device, set `BAMBUDDY_BMCU_STREAM_ACCEPT_NEW=1` and restart Bambuddy;
  after its first heartbeat is registered, set it back to `0` and restart.
- `BMCU_STREAM_QUEUE_SIZE` defaults to 8192 records per device;
  `BMCU_STREAM_BATCH_SIZE` defaults to 512 and `BMCU_STREAM_FLUSH_SECONDS`
  defaults to 0.25. A batch commits before the live state is updated.
- When using Docker bridge mode, publish `8799:8799/udp`; the optional legacy
  listener additionally needs `8799:8799/tcp`.

`GET /api/v1/bmcu-monitors/{device_id}/stream-diagnostics` reports per-device
receiver counters since Bambuddy started, queue depth, heartbeat source and the
latest Pico UDP counters. Malformed envelope rejects are receiver-wide. A
heartbeat is online for three advertised intervals, then stale until 60 seconds,
then offline. The loader state has its own age: it is based on packet arrival,
not the time the batch is committed.

The receiver keeps candidate sequence gaps for two seconds to accommodate UDP
reordering. Candidates and their sequence cursor are committed with the batch,
so candidates survive a Bambuddy restart. A late record shrinks or splits an
inferred gap; a Pico `TRANSPORT_DROP` supersedes the inferred reason. A local
queue overflow is recorded as `RECEIVER_OVERFLOW`; other inferred gaps use
`NETWORK_GAP`. They identify missing data, not its content. UDP losses and data
sent while Bambuddy is down are not retransmitted. ACK and replay values do not
apply to v2 devices. Pico soft reset is performed only from its local interface.

## MQTT alarm delivery

Bambuddy's existing **Settings → Network → MQTT Publishing** connection is used.
When it is enabled and connected, motion-fault set/clear transitions, warning-or-higher
BMCU safety decisions and Pico error/critical logs are sent to
`<mqtt_topic_prefix>/bmcu/alarms` as JSON, QoS 1, without retain. Ordinary printer
transaction warnings are not treated as alarms. `slot` and `link_index` are
zero-based; `kind` distinguishes `motion_fault`, `safety_decision`, and `pico_log`.

Each event contains a stable `event_id`, `device_id`, `pico_boot_id`,
`transport_sequence`, and UTC `received_at`. A motion-fault transition also
contains `active`, `slot`, `previous_value` and `value`. The event and source
record are committed in one transaction. The worker removes the pending status
only after the broker acknowledges publication. PUBACK confirms the broker's
acceptance, not that a device received or acted on it. Delivery is at least
once: clients must persist processed `event_id`s and ignore duplicates. Use a
persistent MQTT subscriber session and appropriate broker configuration if a
client must receive messages while disconnected. Pending events are kept while
the broker is offline; already acknowledged outbox rows follow BMCU retention.
Historical TCP REPLAY records are retained in the log but do not produce fresh
alarm notifications. Pending alarm rows have no automatic expiry, so operators
must monitor the pending count and available disk space during long outages.

`GET /api/v1/bmcu-monitors/{device_id}/alarm-delivery` shows whether MQTT is
enabled/connected, the topic, pending count and the oldest pending time. The
existing feed-stall notification and auto-pause path is separate. In particular,
absence of an alarm does not mean the link is healthy: monitor heartbeat and
transport gaps independently. UDP data lost before Bambuddy receives it cannot
be recovered by this outbox. For a non-loss-tolerant alarm, the source must
latch it or support reliable replay. Existing MQTT TLS settings permit unverified
certificates; use a trusted network unless certificate verification is configured.

In addition, the feed-stall watcher queues `feed_stall_warning`,
`feed_stall_detected`, and `feed_stall_pause_confirmed` events. These are
server-derived events, with `origin: "bambuddy"` and a null transport sequence;
their stable event IDs identify an episode and stage. They enter a bounded
in-memory queue before the outbox commit, so they can be lost on a crash before
commit or if that queue is full. The queue depth and overflow counter are shown
by the delivery-status endpoint. A printer pause is never held up while waiting
for the database or broker. Feed-stall monitoring requires explicit
`BAMBUDDY_FEED_STALL_DEVICE_ID`, `BAMBUDDY_FEED_STALL_LINK_ID` (numeric index,
including `0`), and the intended `BAMBUDDY_FEED_STALL_PRINTER_ID`; missing or
ambiguous mapping leaves monitoring unarmed.
The current watcher maps only one BMCU link per printer. It does not yet
support the installation where two BMCUs feed the same printer; keep automatic
pause unarmed there until active-link selection and transitions are validated.
For TCP, the current session must report a non-replayed LINK_STATE of online,
followed by a fresh STATUS. A device that does not report that LINK_STATE does
not arm the watcher, even if it sends STATUS records.

A retained health snapshot is published every five seconds to
`<prefix>/bmcu/<percent-encoded-device-id>/status`. The payload includes the
Pico boot ID, link availability, STATUS ages, cumulative transport losses,
`observed_at` and `valid_until` in UTC. Consumers must treat it as **unknown**
after `valid_until`, or when the retained `<prefix>/status` reports Bambuddy
as offline. Each link also has its own `valid_until`, bounded by both heartbeat
and STATUS freshness; after that time the link is unknown. Counter totals reset
when `receiver_started_at` changes. This prevents an old retained `online` message from representing a
healthy system after a crash or network partition. A broker acknowledgment is
not a siren or speaker acknowledgment; an end-to-end wake-up system still needs
the external device, its persistent session, and a tested audible alert.

If UDP reorders a fault-clear and fault-set event, MQTT delivery may reflect the
receive order rather than the source order. Subscribers must deduplicate by
`event_id`, compare `transport_sequence` per `(device_id, pico_boot_id,
link_index, slot)`, and never allow an older sequence or prior boot to override
newer state. Derived Bambuddy events have no transport sequence and must be
handled as separate episode notifications.

For an Amazon Echo, Home Assistant can subscribe to the alarm topic and use an
Alexa Devices notify entity to announce the message. Gate the automation with a
manually enabled helper so it sounds only during a monitoring session. The
broker, Alexa integration, Echo entity, subscriber deduplication and audible
end-to-end test must be set up separately; this repository does not enable an
automation or claim that a broker acknowledgment means the Echo sounded.
See [Alexa Devices](https://www.home-assistant.io/integrations/alexa_devices/)
and [MQTT triggers](https://www.home-assistant.io/docs/automation/trigger/#mqtt-trigger).
