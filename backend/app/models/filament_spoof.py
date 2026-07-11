"""Model for the filament-spoof runout-backup feature.

Bambu firmware's AMS Filament Backup only auto-switches between slots whose
``tray_info_idx`` AND ``tray_color`` are identical. To let an arbitrary
same-material / different-color spool back up a near-empty one, bambuddy writes
the *primary* slot's identity onto the *backup* slot (the "spoof") via
``ams_filament_setting``, while remembering the backup slot's REAL identity here
so bambuddy's own state (UI / scheduler / spoolman / virtual-printer) keeps
showing the truth.

Each row is one engaged spoof. When RELEASED the row is retained for audit /
revalidation but no longer overlaid.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class FilamentSpoof(Base):
    """A single primary→backup filament spoof binding for one printer."""

    __tablename__ = "filament_spoofs"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id"), index=True)

    # The slot whose identity is being overwritten (the backup spool).
    backup_ams_id: Mapped[int] = mapped_column(Integer)
    backup_tray_id: Mapped[int] = mapped_column(Integer)

    # The slot whose identity is being impersonated (the primary, near-empty spool).
    primary_ams_id: Mapped[int] = mapped_column(Integer)
    primary_tray_id: Mapped[int] = mapped_column(Integer)

    # The backup slot's REAL identity (snapshot at engage time), restored into
    # bambuddy's overlaid state so downstream readers see truth.
    real_tray_info_idx: Mapped[str | None] = mapped_column(String(50), nullable=True)
    real_tray_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    real_tray_sub_brands: Mapped[str | None] = mapped_column(String(100), nullable=True)
    real_tray_color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    real_nozzle_temp_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    real_nozzle_temp_max: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The backup slot's REAL calibration (K profile) snapshot, so the spoof
    # identity write doesn't clobber the slot's K profile (design rule C).
    # Re-asserted after the identity write and restored on release.
    real_cali_idx: Mapped[str | None] = mapped_column(String(16), nullable=True)
    real_k: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Extruder the backup slot belongs to at engage time (H2D-class). Used only
    # for diagnostics; the same-extruder check runs against live state.
    extruder_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # The spoofed identity written onto the backup slot's firmware (= primary's).
    # Used as the fail-safe match key: we only overlay when the live firmware
    # tray still shows exactly this identity.
    spoof_tray_info_idx: Mapped[str | None] = mapped_column(String(50), nullable=True)
    spoof_tray_color: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # PENDING  : firmware write sent, waiting for the backup slot to report the
    #            spoofed identity (BMCU writes can silently fail — see engine).
    # ENGAGED  : firmware confirmed the spoofed identity; overlay active.
    # RELEASED : intentionally released (audit-retained, never re-overlaid).
    # FAILED   : firmware never confirmed within the timeout; guard removed.
    state: Mapped[str] = mapped_column(String(16), default="PENDING")
    engaged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
