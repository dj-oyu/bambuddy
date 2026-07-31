"""Persistence for authenticated BMCU Binary Transport v1."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class BMCUBinaryDevice(Base):
    __tablename__ = "bmcu_binary_devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    firmware: Mapped[str] = mapped_column(String(63), nullable=False)
    pico_boot_id: Mapped[str] = mapped_column(String(16), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_ack_sequence: Mapped[str] = mapped_column(String(20), nullable=False, default="00000000000000000000")
    oldest_available_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    newest_available_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    control_sequence: Mapped[str] = mapped_column(String(20), nullable=False, default="00000000000000000000")


class BMCUBinaryBoot(Base):
    __tablename__ = "bmcu_binary_boots"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(63), nullable=False)
    pico_boot_id: Mapped[str] = mapped_column(String(16), nullable=False)
    last_ack_sequence: Mapped[str] = mapped_column(String(20), nullable=False, default="00000000000000000000")
    oldest_available_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    newest_available_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("device_id", "pico_boot_id", name="uq_bmcu_binary_boot"),)


class BMCUBinaryLink(Base):
    __tablename__ = "bmcu_binary_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(63), nullable=False)
    link_index: Mapped[int] = mapped_column(Integer, nullable=False)
    link_id: Mapped[str] = mapped_column(String(31), nullable=False)

    __table_args__ = (
        UniqueConstraint("device_id", "link_index", name="uq_bmcu_binary_link_index"),
        UniqueConstraint("device_id", "link_id", name="uq_bmcu_binary_link_id"),
    )


class BMCUBinaryRecord(Base):
    __tablename__ = "bmcu_binary_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(63), nullable=False)
    pico_boot_id: Mapped[str] = mapped_column(String(16), nullable=False)
    transport_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    link_index: Mapped[int] = mapped_column(Integer, nullable=False)
    flags: Mapped[int] = mapped_column(Integer, nullable=False)
    message_type: Mapped[int] = mapped_column(Integer, nullable=False)
    received_at_us: Mapped[str | None] = mapped_column(String(20))
    server_received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    bmcu_version: Mapped[int | None] = mapped_column(Integer)
    bmcu_kind: Mapped[int | None] = mapped_column(Integer)
    bmcu_sequence: Mapped[int | None] = mapped_column(Integer)
    raw_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    raw_bmcu_frame: Mapped[bytes | None] = mapped_column(LargeBinary)

    __table_args__ = (
        UniqueConstraint("device_id", "pico_boot_id", "transport_sequence", name="uq_bmcu_binary_identity"),
        Index("ix_bmcu_binary_device_time", "device_id", "server_received_at"),
        Index("ix_bmcu_binary_device_kind", "device_id", "message_type", "server_received_at"),
    )


class BMCUBinaryDiagnostic(Base):
    __tablename__ = "bmcu_binary_diagnostics"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(63), nullable=False)
    pico_boot_id: Mapped[str] = mapped_column(String(16), nullable=False)
    transport_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (Index("ix_bmcu_binary_diag_device_time", "device_id", "recorded_at"),)


class BMCUBinaryLog(Base):
    __tablename__ = "bmcu_binary_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(63), nullable=False)
    pico_boot_id: Mapped[str] = mapped_column(String(16), nullable=False)
    transport_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    log_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    uptime_ms: Mapped[str] = mapped_column(String(20), nullable=False)
    severity: Mapped[int] = mapped_column(Integer, nullable=False)
    component: Mapped[str] = mapped_column(String(40), nullable=False)
    message: Mapped[str] = mapped_column(String(320), nullable=False)
    detail: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_bmcu_binary_log_device_time", "device_id", "recorded_at"),
        Index("ix_bmcu_binary_log_severity", "device_id", "severity", "recorded_at"),
        Index("ix_bmcu_binary_log_component", "device_id", "component", "recorded_at"),
    )


class BMCUBinaryControlResult(Base):
    __tablename__ = "bmcu_binary_control_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(63), nullable=False)
    pico_boot_id: Mapped[str] = mapped_column(String(16), nullable=False)
    command_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    result: Mapped[int] = mapped_column(Integer, nullable=False)
    detail: Mapped[str] = mapped_column(String(160), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("device_id", "pico_boot_id", "command_sequence", name="uq_bmcu_binary_control_result"),
        Index("ix_bmcu_binary_control_device_time", "device_id", "recorded_at"),
    )


class BMCUBinaryLossRange(Base):
    __tablename__ = "bmcu_binary_loss_ranges"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(63), nullable=False)
    pico_boot_id: Mapped[str] = mapped_column(String(16), nullable=False)
    report_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    first_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    last_sequence: Mapped[str] = mapped_column(String(20), nullable=False)
    dropped_count: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("device_id", "pico_boot_id", "report_sequence", name="uq_bmcu_binary_loss_report"),
        Index("ix_bmcu_binary_loss_boot_range", "device_id", "pico_boot_id", "first_sequence"),
    )
