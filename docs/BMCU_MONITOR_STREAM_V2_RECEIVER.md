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
