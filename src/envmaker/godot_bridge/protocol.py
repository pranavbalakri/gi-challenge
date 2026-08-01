"""Length-prefixed JSON framing for Python/Godot bridge messages."""

from __future__ import annotations

import json as _json
import struct as _struct
from typing import TYPE_CHECKING as _TYPE_CHECKING

from envmaker.core.contracts import (
    MAX_CONTROL_MESSAGE_BYTES as _MAX_CONTROL_MESSAGE_BYTES,
)

if _TYPE_CHECKING:
    from envmaker.core.contracts import BridgeRequest, BridgeResponse

__all__ = [
    "FRAME_HEADER_BYTES",
    "FramingError",
    "encode_frame",
    "decode_body",
    "FrameDecoder",
]

FRAME_HEADER_BYTES: int = 4


class FramingError(ValueError):
    """Raised when a bridge frame cannot be encoded or decoded."""


def encode_frame(message: BridgeRequest | BridgeResponse) -> bytes:
    """Encode one bridge envelope as a length-prefixed JSON frame."""
    body = _json.dumps(
        message.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(body) > _MAX_CONTROL_MESSAGE_BYTES:
        raise FramingError("frame exceeds control message limit")
    return _struct.pack(">I", len(body)) + body


def decode_body(body: bytes) -> dict[str, object]:
    """Decode a JSON frame body without validating its envelope."""
    try:
        message = _json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError):
        raise FramingError("malformed json frame") from None
    if not isinstance(message, dict):
        raise FramingError("frame must be a json object")
    return message


class FrameDecoder:
    """Incrementally reassemble length-prefixed JSON frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._failed = False
        self._last_batch_body_lengths: tuple[int, ...] = ()

    def feed(self, chunk: bytes) -> list[dict[str, object]]:
        """Buffer a chunk and return every frame it completes."""
        if self._failed:
            raise FramingError("framing decoder already failed")

        self._last_batch_body_lengths = ()
        self._buffer.extend(chunk)
        decoded: list[dict[str, object]] = []
        body_lengths: list[int] = []
        try:
            while len(self._buffer) >= FRAME_HEADER_BYTES:
                (body_length,) = _struct.unpack(
                    ">I",
                    self._buffer[:FRAME_HEADER_BYTES],
                )
                if body_length == 0:
                    raise FramingError("empty frame")
                if body_length > _MAX_CONTROL_MESSAGE_BYTES:
                    raise FramingError("frame exceeds control message limit")

                frame_length = FRAME_HEADER_BYTES + body_length
                if len(self._buffer) < frame_length:
                    break

                body = bytes(self._buffer[FRAME_HEADER_BYTES:frame_length])
                del self._buffer[:frame_length]
                decoded.append(decode_body(body))
                body_lengths.append(body_length)
        except FramingError:
            self._failed = True
            raise

        self._last_batch_body_lengths = tuple(body_lengths)
        return decoded

    @property
    def buffered_bytes(self) -> int:
        """Return the number of bytes waiting for a complete frame."""
        return len(self._buffer)

    @property
    def last_batch_body_lengths(self) -> tuple[int, ...]:
        """Return encoded body lengths for frames decoded by the last feed."""
        return self._last_batch_body_lengths
