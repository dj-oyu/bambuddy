from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class BMCULinkDevice(Base):
    """BMCU Link bridge device (Pico 2 W pushing bmcu.management.v2 envelopes)."""

    __tablename__ = "bmcu_link_devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    firmware: Mapped[str | None] = mapped_column(String(50), nullable=True)
    protocol_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    protocol_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capabilities: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    mode: Mapped[str | None] = mapped_column(String(30), nullable=True)  # production_monitor | bench_stub
    link_state: Mapped[str] = mapped_column(String(10), nullable=False, default="offline")  # online|stale|offline
    pico_boot_session: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bmcu_boot_session: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON of most recent status frame data
    envelope_count: Mapped[int] = mapped_column(Integer, default=0)
    dropped_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # CONTROL security contract (firmware issue #2). The control key is a
    # shared secret provisioned to both this row and the Pico commissioning
    # UI; Fernet-encrypted at rest (provisioning fails closed when no
    # encryption key is configured), never returned after create, never
    # logged. NULL = CONTROL not provisioned (fail-safe).
    control_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    control_key_set_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Monotonic control_sequence allocator state, persisted so a bambuddy
    # restart cannot reuse a sequence within a live Pico session. Counter
    # resets to 0 when the session nonce changes (new Pico boot/session).
    control_session_nonce: Mapped[str | None] = mapped_column(String(100), nullable=True)
    control_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
