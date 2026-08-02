"""Codec exceptions. Error text must not contain authentication material."""


class BinaryProtocolError(ValueError):
    """Base error for malformed BMB1 input."""


class InvalidHeader(BinaryProtocolError):
    pass


class PayloadTooLarge(BinaryProtocolError):
    pass


class InvalidMessage(BinaryProtocolError):
    pass


class AuthenticationError(BinaryProtocolError):
    pass


class InvalidBMCUFrame(BinaryProtocolError):
    pass


class InvalidTransportDrop(BinaryProtocolError):
    pass
