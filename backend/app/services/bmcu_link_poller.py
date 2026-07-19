"""BMCU Link Pico /api/status poller (private fork).

The alpha.3 Pico firmware only serves a read-only ``GET /api/status`` JSON
snapshot (single-connection HTTP server, no push transport yet). This poller
fetches that snapshot and translates it into synthetic ``bmcu.management.v2``
envelopes fed through the existing :class:`BMCULinkService` ingest pipeline,
so device tracking, WS relay, watchdog, DB event rows and notifications all
work with a real Pico today. The push ingest routes stay untouched; once the
firmware grows a real push envelope this poller becomes redundant.

Toggle: BAMBUDDY_BMCU_LINK_POLL_URL (unset/empty = poller never starts).
"""

import asyncio
import json
import logging
import random
import time
import uuid
from collections import deque
from urllib.parse import urlsplit

import httpx

from backend.app.schemas.bmcu_link import EXPECTED_SCHEMA, BMCULinkEnvelope

logger = logging.getLogger(__name__)


def bmcu_link_poll_url() -> str | None:
    """Pico status URL from env; None disables the poller entirely."""
    import os

    raw = os.environ.get("BAMBUDDY_BMCU_LINK_POLL_URL", "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = f"http://{raw}"
    if urlsplit(raw).path in ("", "/"):
        raw = raw.rstrip("/") + "/api/status"
    return raw


def bmcu_link_poll_interval() -> float:
    import os

    try:
        v = float(os.environ.get("BAMBUDDY_BMCU_LINK_POLL_INTERVAL", ""))
    except ValueError:
        return BMCULinkPoller.INTERVAL_DEFAULT
    return min(max(v, BMCULinkPoller.INTERVAL_MIN), BMCULinkPoller.INTERVAL_MAX)


class BMCULinkPoller:
    INTERVAL_DEFAULT = 2.0
    INTERVAL_MIN = 1.0
    INTERVAL_MAX = 8.0  # must stay well under BMCULinkService.STALE_AFTER_S
    CONNECT_TIMEOUT = 3.0
    READ_TIMEOUT = 5.0
    MAX_BACKOFF_S = 60.0
    MAX_BODY_BYTES = 262_144
    SEEN_EVENTS_WINDOW = 64  # 4 generations of the Pico's 16-entry event ring
    STATUS_HEARTBEAT_S = 60.0
    # Analog fields that change on nearly every poll; excluding them from the
    # change key keeps DB row volume bounded during long prints.
    STATUS_VOLATILE_FIELDS = ("hw_tick32", "pressure", "pull_pct")
    CHANNEL_VOLATILE_FIELDS = ("raw_angle", "position_delta", "motor_pwm", "pull_pct")
    ANOMALY_SEVERITY_MIN = 4  # firmware severity enum: 4 error, 5 critical

    def __init__(
        self,
        url: str,
        device_id: str | None = None,
        interval: float | None = None,
        service=None,
        fetch=None,  # async () -> dict; test seam replacing httpx
        clock=time.monotonic,
    ) -> None:
        self.url = url
        self.device_id = device_id or f"pico-{urlsplit(url).hostname or 'unknown'}"
        self.interval = interval if interval is not None else bmcu_link_poll_interval()
        self._service = service
        self._fetch = fetch or self._fetch_http
        self._clock = clock
        self._client: httpx.AsyncClient | None = None
        self._boot_session = f"poll-{uuid.uuid4().hex[:8]}"
        self._seq = 0
        self._hello_sent = False
        self._last_status_key: str | None = None
        self._last_status_emit = 0.0
        self._seen_events: deque[tuple] = deque(maxlen=self.SEEN_EVENTS_WINDOW)
        self._seen_events_set: set[tuple] = set()
        self._ring_was_nonempty = False
        self._prev_bmcu_link: str | None = None
        self._fail_count = 0

    def _get_service(self):
        if self._service is None:
            from backend.app.services.bmcu_link import bmcu_link_service

            self._service = bmcu_link_service
        return self._service

    # ------------------------------------------------------------------ http

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # The Pico HTTP server handles one connection at a time and has
            # leak-prone error paths: force a fresh, explicitly closed
            # connection per request and keep timeouts tight.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=self.CONNECT_TIMEOUT,
                    read=self.READ_TIMEOUT,
                    write=self.CONNECT_TIMEOUT,
                    pool=self.CONNECT_TIMEOUT,
                ),
                limits=httpx.Limits(max_keepalive_connections=0, max_connections=1),
                headers={"Connection": "close"},
            )
        return self._client

    async def _fetch_http(self) -> dict:
        resp = await self._get_client().get(self.url)
        resp.raise_for_status()
        if len(resp.content) > self.MAX_BODY_BYTES:
            raise ValueError(f"response too large ({len(resp.content)} bytes)")
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("response is not a JSON object")
        return body

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------- envelopes

    def _make_envelope(self, kind: str, data: dict, link_state: str) -> dict:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return {
            "schema": EXPECTED_SCHEMA,
            "device_id": self.device_id,
            "received_at_us": time.time_ns() // 1000,
            "link": {
                "state": link_state,
                "transport_sequence": self._seq,
                "uart_sequence": self._seq,
                "pico_boot_session": self._boot_session,
                # /api/status exposes no BMCU boot identity; hw_tick32 wrap
                # (~238.6 s) makes reboot inference unreliable, so pin to 0.
                "bmcu_boot_session": 0,
            },
            "frame": {"kind": kind},
            "data": data,
        }

    def _status_key(self, status_data: dict) -> str:
        """Change-detection key over the full emitted status payload, minus
        per-poll analog noise (ticks, pressure, encoder/PWM samples)."""
        trimmed = {k: v for k, v in status_data.items() if k not in self.STATUS_VOLATILE_FIELDS}
        channels = trimmed.get("channels")
        if isinstance(channels, list):
            trimmed["channels"] = [
                {k: v for k, v in ch.items() if k not in self.CHANNEL_VOLATILE_FIELDS}
                if isinstance(ch, dict)
                else ch
                for ch in channels
            ]
        return json.dumps(trimmed, sort_keys=True, default=str)

    @staticmethod
    def _event_fingerprint(ev: dict) -> tuple:
        return (ev.get("hw_tick32"), ev.get("record_type"), ev.get("source"), ev.get("payload"))

    def _translate(self, body: dict) -> tuple[list[dict], dict]:
        """Snapshot → (envelope dicts, staged state). Poller state is only
        advanced by :meth:`_commit` AFTER a successful ingest, so a transient
        ingest failure re-emits the same events instead of losing them
        (mirrors the service's record-dedup-key-after-staging discipline)."""
        envelopes: list[dict] = []
        bmcu = body.get("bmcu") or {}
        wifi = body.get("wifi") or {}
        link_state = bmcu.get("link") or "online"
        status = bmcu.get("status") or {}
        events = bmcu.get("events") or []

        if not self._hello_sent:
            envelopes.append(
                self._make_envelope(
                    "hello",
                    {"mode": "poll", "capabilities": ["poll"], "tick_hz": bmcu.get("tick_hz")},
                    link_state,
                )
            )

        # Pico↔BMCU UART going stale is an anomaly even though the Pico
        # itself (and therefore this device) stays online.
        if link_state == "stale" and self._prev_bmcu_link not in (None, "stale"):
            envelopes.append(self._make_envelope("anomaly", {"reason": "bmcu_uart_stale"}, link_state))
        recovered = link_state != "stale" and self._prev_bmcu_link == "stale"

        # Event-ring turnover after non-empty usually means a Pico reboot;
        # forget fingerprints so the next generation is not misdeduped.
        ring_reset = not events and self._ring_was_nonempty
        seen = set() if ring_reset else self._seen_events_set
        new_fingerprints: list[tuple] = []
        staged_seen: set[tuple] = set()
        for ev in events:
            if not isinstance(ev, dict):
                continue
            fp = self._event_fingerprint(ev)
            if fp in seen or fp in staged_seen:
                continue
            new_fingerprints.append(fp)
            staged_seen.add(fp)
            severity = ev.get("severity")
            kind = "anomaly" if isinstance(severity, int) and severity >= self.ANOMALY_SEVERITY_MIN else "event"
            envelopes.append(self._make_envelope(kind, ev, link_state))

        data = dict(status)
        data["link"] = link_state
        data["wifi_state"] = wifi.get("state")
        data["channels"] = bmcu.get("channels")
        data["decoder_crc_errors"] = bmcu.get("decoder_crc_errors")
        data["decoder_frame_errors"] = bmcu.get("decoder_frame_errors")
        status_key = self._status_key(data)
        now = self._clock()
        status_emitted = (
            status_key != self._last_status_key
            or recovered
            or now - self._last_status_emit >= self.STATUS_HEARTBEAT_S
        )
        if status_emitted:
            envelopes.append(self._make_envelope("status", data, link_state))

        staged = {
            "link_state": link_state,
            "ring_reset": ring_reset,
            "ring_nonempty": bool(events),
            "new_fingerprints": new_fingerprints,
            "status_key": status_key if status_emitted else None,
            "status_emit_at": now if status_emitted else None,
        }
        return envelopes, staged

    def _commit(self, staged: dict) -> None:
        self._prev_bmcu_link = staged["link_state"]
        if staged["ring_reset"]:
            self._seen_events.clear()
            self._seen_events_set.clear()
        if staged["ring_nonempty"]:
            self._ring_was_nonempty = True
        elif staged["ring_reset"]:
            self._ring_was_nonempty = False
        for fp in staged["new_fingerprints"]:
            if len(self._seen_events) == self._seen_events.maxlen:
                self._seen_events_set.discard(self._seen_events[0])
            self._seen_events.append(fp)
            self._seen_events_set.add(fp)
        if staged["status_key"] is not None:
            self._last_status_key = staged["status_key"]
            self._last_status_emit = staged["status_emit_at"]

    # ----------------------------------------------------------------- loop

    async def poll_once(self) -> bool:
        """One poll cycle; returns success. Never raises."""
        try:
            body = await self._fetch()
        except Exception as e:
            self._fail_count += 1
            level = logging.WARNING if self._fail_count == 1 else logging.DEBUG
            logger.log(level, "BMCU Link poll of %s failed (%d consecutive): %s", self.url, self._fail_count, e)
            return False
        try:
            if self._fail_count:
                # Force a status emit after an outage so the device's
                # offline→online transition is persisted, not just marked.
                self._last_status_key = None
                self._fail_count = 0
            envelopes, staged = self._translate(body)
            service = self._get_service()
            if envelopes:
                parsed = [BMCULinkEnvelope.model_validate(e) for e in envelopes]
                await service.ingest(parsed)
                self._hello_sent = True
            else:
                service.mark_seen(self.device_id)
            self._commit(staged)
        except Exception:
            logger.exception("BMCU Link poll translation/ingest failed for %s", self.url)
        return True

    def _next_delay(self) -> float:
        if self._fail_count == 0:
            return self.interval
        backoff = min(self.interval * (2 ** min(self._fail_count, 5)), self.MAX_BACKOFF_S)
        return backoff + random.uniform(0, self.interval / 2)

    async def run(self) -> None:
        logger.info("BMCU Link poller started for %s (device_id=%s, interval=%.1fs)", self.url, self.device_id, self.interval)
        while True:
            try:
                await self.poll_once()
                await asyncio.sleep(self._next_delay())
            except asyncio.CancelledError:
                break
