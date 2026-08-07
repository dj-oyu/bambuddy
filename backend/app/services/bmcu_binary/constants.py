"""Generated-shape constants matching docs/bmcu_binary_registry.json."""

from enum import IntEnum, IntFlag

MAGIC = b"BMB1"
VERSION = 1
HEADER_SIZE = 32
MAX_PAYLOAD_SIZE = 4096


class MessageType(IntEnum):
    SERVER_CHALLENGE = 0x01
    HELLO = 0x02
    HELLO_ACCEPTED = 0x03
    BMCU_FRAME = 0x10
    LINK_STATE = 0x11
    TRANSPORT_DROP = 0x12
    PICO_DIAGNOSTIC = 0x13
    PICO_LOG = 0x14
    ACK = 0x20
    CONTROL = 0x30
    CONTROL_RESULT = 0x31
    PING = 0x40
    PONG = 0x41
    PROTOCOL_ERROR = 0x7F


class Flag(IntFlag):
    REPLAY = 1 << 0
    CRITICAL = 1 << 1
    SNAPSHOT = 1 << 2
    WALL_TIME_VALID = 1 << 3
    JOURNALED = 1 << 4


class LinkState(IntEnum):
    UNKNOWN = 0
    RESYNCING = 1
    ONLINE = 2
    STALE = 3
    OFFLINE = 4
    INCOMPATIBLE = 5


class LinkReason(IntEnum):
    UNSPECIFIED = 0
    FRAME_RECEIVED = 1
    RESPONSE_TIMEOUT = 2
    UART_ERROR = 3
    PROTOCOL_VERSION = 4
    RESYNC_REQUESTED = 5
    DEVICE_REBOOT = 6


class LogSeverity(IntEnum):
    DEBUG = 0
    INFO = 1
    NOTICE = 2
    WARNING = 3
    ERROR = 4
    CRITICAL = 5


class RecordType(IntEnum):
    """EVENT `record_type`. Only STATE_CHANGE carries the field/slot union."""

    BOOT = 1
    PRINTER_LINK = 2
    PRINTER_TRANSACTION = 3
    STATE_CHANGE = 4
    SENSOR = 5
    COMMAND_RESULT = 6
    SAFETY_DECISION = 7
    DIAGNOSTIC_COUNTER = 8
    PRINTER_LONG_TRANSACTION = 9
    RESET_STATE = 10


class StateField(IntEnum):
    """Which field a STATE_CHANGE event reports, from the link registry.

    The names matter downstream: the timeline used to collapse every
    state_change into one indistinguishable "state change" series, so a
    MOTION_FAULT latching on a channel looked exactly like a SLOT selection —
    2026-08-07, when a channel-1 motion fault sat latched through two feed
    pauses and had to be recovered by hand-decoding stored frames.
    """

    SLOT = 1
    INSERTED_MASK = 2
    ONLINE_MASK = 3
    MOTION = 4
    PRESSURE = 5
    LED_MODE = 6
    CONTROL_ERROR = 7
    MOTION_FAULT = 8


def state_field_name(field: int | None) -> str | None:
    """Registry name for a STATE_CHANGE field, or None when unnamed.

    None is "this firmware named a field we do not know", which must stay
    distinguishable from a field we chose not to label.
    """
    if field is None:
        return None
    try:
        return StateField(field).name.lower()
    except ValueError:
        return None


class ValueType(IntEnum):
    UINT8 = 1
    UINT16 = 2
    UINT32 = 3
    UINT64 = 4
    INT8 = 5
    INT16 = 6
    INT32 = 7
    INT64 = 8
    BOOL = 9
    UTF8 = 10
    BYTES = 11


class DropReason(IntEnum):
    UNKNOWN = 0
    RAM_QUEUE_FULL = 1
    JOURNAL_FULL = 2
    JOURNAL_CORRUPT = 3
    RECORD_TOO_LARGE = 4
    RETENTION_EVICTION = 5


class RejectReason(IntEnum):
    UNKNOWN = 0
    MALFORMED = 1
    UNSUPPORTED = 2
    INVALID_BMCU_FRAME = 3
    PERSISTENCE_FAILED = 4


class ControlCommand(IntEnum):
    SOFT_RESET = 1


class ControlResultCode(IntEnum):
    OK = 0
    EXPIRED = 1
    REPLAYED = 2
    UNAUTHENTICATED = 3
    UNKNOWN_COMMAND = 4
    UNSAFE = 5
    INVALID_ARGUMENT = 6
    INTERNAL_ERROR = 7


class ProtocolErrorCode(IntEnum):
    MALFORMED_HEADER = 1
    PAYLOAD_TOO_LARGE = 2
    AUTHENTICATION_FAILED = 3
    UNEXPECTED_MESSAGE = 4
    INVALID_PAYLOAD = 5
    REPLAYED_CONTROL = 6
