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
