"""Bounded, dependency-free codec for BMCU Binary Transport v1."""

from .framing import Frame, FrameHeader, IncrementalFrameParser, encode_frame

__all__ = ["Frame", "FrameHeader", "IncrementalFrameParser", "encode_frame"]
