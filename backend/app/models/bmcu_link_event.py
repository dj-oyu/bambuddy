from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class BMCULinkEvent(Base):
    """One ingested BMCU Link envelope (observe-only event stream).

    Dedup tuple (device_id, link_id, pico_boot_session, transport_sequence)
    is enforced in-memory by the service — deliberately NO unique constraint
    here so a dedup-window miss degrades to a duplicate row, never an insert
    failure that could poison a whole flush batch.
    """

    __tablename__ = "bmcu_link_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(100), nullable=False)
    link_id: Mapped[str] = mapped_column(String(50), nullable=False, default="default", server_default="default")
    pico_boot_session: Mapped[str] = mapped_column(String(100), nullable=False)
    bmcu_boot_session: Mapped[int] = mapped_column(Integer, nullable=False)
    uart_sequence: Mapped[int] = mapped_column(Integer, nullable=False)  # BMCU u16 seq, diagnostics only
    transport_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # Pico u64, dedup key
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    kind_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    protocol: Mapped[int | None] = mapped_column(Integer, nullable=True)
    received_at_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # Pico wall clock, informational
    server_received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # UTC, authoritative ordering
    transaction_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)  # raw JSON of the envelope "data" field

    __table_args__ = (
        Index("ix_bmcu_events_device_time", "device_id", "server_received_at"),
        Index("ix_bmcu_events_device_kind", "device_id", "kind", "server_received_at"),
        Index("ix_bmcu_events_txn", "device_id", "transaction_id"),
    )
