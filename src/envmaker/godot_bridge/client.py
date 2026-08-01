"""Authenticated synchronous client side of the Godot bridge transport."""

from __future__ import annotations

import socket as _socket
import time as _time
from typing import NoReturn as _NoReturn

from pydantic import ValidationError as _ValidationError

from envmaker.core.contracts import (
    DEFAULT_DEADLINE_SECONDS as _DEFAULT_DEADLINE_SECONDS,
    MAX_IN_FLIGHT_BYTES as _MAX_IN_FLIGHT_BYTES,
    SIMULATION_TYPES as _SIMULATION_TYPES,
    BridgeRequest as _BridgeRequest,
    BridgeResponse as _BridgeResponse,
    MessageType as _MessageType,
)
from envmaker.core.signals import Signal as _Signal
from envmaker.core.signals import SignalSeverity as _SignalSeverity
from envmaker.godot_bridge.protocol import FrameDecoder as _FrameDecoder
from envmaker.godot_bridge.protocol import encode_frame as _encode_frame

__all__ = [
    "BridgeError",
    "BridgeTimeoutError",
    "BridgeProtocolError",
    "BridgeSession",
    "BridgeServer",
]

_SOCKET_POLL_SECONDS = 0.1
_RECV_BYTES = 65_536


def _await_bytes_exceeded(consumed: int) -> bool:
    return consumed > _MAX_IN_FLIGHT_BYTES


class BridgeError(RuntimeError):
    """Base error for bridge transport operations."""


class BridgeTimeoutError(BridgeError):
    """Raised when a bridge operation exceeds its deadline."""


class BridgeProtocolError(BridgeError):
    """Raised when the bridge stream violates the protocol."""


class BridgeSession:
    """One authenticated synchronous bridge connection."""

    def __init__(
        self,
        *,
        connection: _socket.socket,
        session_id: str,
        protocol_version: int,
        decoder: _FrameDecoder,
        queued_bodies: list[dict[str, object]],
    ) -> None:
        self._connection = connection
        self._session_id = session_id
        self._protocol_version = protocol_version
        self._decoder = decoder
        self._queued_bodies = queued_bodies
        self._next_request_id = 1
        self._last_tick: int | None = None
        self._closed = False
        self._await_bytes = 0

    def request(
        self,
        type: _MessageType,
        payload: dict[str, object] | None = None,
        *,
        tick_id: int | None = None,
        deadline: float | None = None,
    ) -> _BridgeResponse:
        """Send one request and synchronously return its correlated response."""
        if self._closed:
            raise BridgeError("session closed")

        request_id = self._next_request_id
        request = _BridgeRequest(
            protocol_version=self._protocol_version,
            session_id=self._session_id,
            request_id=request_id,
            tick_id=tick_id,
            type=type,
            payload={} if payload is None else payload,
        )
        try:
            frame = _encode_frame(request)
        except Exception:
            self._fatal(BridgeProtocolError, "framing failure")

        self._next_request_id += 1
        timeout = (
            _DEFAULT_DEADLINE_SECONDS if deadline is None else deadline
        )
        end = _time.monotonic() + timeout
        remaining = end - _time.monotonic()
        if remaining <= 0:
            self._fatal(
                BridgeTimeoutError,
                "request deadline exceeded",
            )

        try:
            self._connection.settimeout(remaining)
            self._connection.sendall(frame)
        except _socket.timeout:
            self._fatal(
                BridgeTimeoutError,
                "request deadline exceeded",
            )
        except OSError:
            self._fatal(
                BridgeProtocolError,
                "connection closed by runner",
            )

        self._await_bytes = 0
        body = self._receive_body(end)
        try:
            response = _BridgeResponse.model_validate(body)
        except _ValidationError:
            self._fatal(
                BridgeProtocolError,
                "invalid response envelope",
            )

        if response.request_id != request_id:
            self._fatal(
                BridgeProtocolError,
                "response correlation mismatch",
            )
        if response.session_id != self._session_id:
            self._fatal(BridgeProtocolError, "session mismatch")
        if response.type is not type:
            self._fatal(
                BridgeProtocolError,
                "response correlation mismatch",
            )
        if response.tick_id != tick_id:
            self._fatal(
                BridgeProtocolError,
                "response correlation mismatch",
            )

        if response.ok and type in _SIMULATION_TYPES:
            self._last_tick = tick_id
        return response

    def close(self, deadline: float | None = None) -> None:
        """Best-effort the close exchange, then always close the transport."""
        if self._closed:
            return

        close_deadline = min(5.0, deadline or 5.0)
        try:
            self.request(_MessageType.CLOSE, deadline=close_deadline)
        except BridgeError:
            pass
        finally:
            self._close_transport()

    @property
    def closed(self) -> bool:
        """Return whether this session can no longer be used."""
        return self._closed

    @property
    def last_tick(self) -> int | None:
        """Return the last successfully acknowledged simulation tick."""
        return self._last_tick

    @property
    def session_id(self) -> str:
        """Return the authenticated session identifier."""
        return self._session_id

    def _receive_body(self, end: float) -> dict[str, object]:
        if self._queued_bodies:
            return self._queued_bodies.pop(0)

        while True:
            remaining = end - _time.monotonic()
            if remaining <= 0:
                self._fatal(
                    BridgeTimeoutError,
                    "request deadline exceeded",
                )

            try:
                self._connection.settimeout(
                    min(_SOCKET_POLL_SECONDS, remaining)
                )
                chunk = self._connection.recv(_RECV_BYTES)
            except _socket.timeout:
                continue
            except OSError:
                self._fatal(
                    BridgeProtocolError,
                    "connection closed by runner",
                )

            if not chunk:
                self._fatal(
                    BridgeProtocolError,
                    "connection closed by runner",
                )

            self._await_bytes += len(chunk)
            if _await_bytes_exceeded(self._await_bytes):
                self._fatal(
                    BridgeProtocolError,
                    "in-flight byte limit exceeded",
                )

            try:
                bodies = self._decoder.feed(chunk)
            except Exception:
                self._fatal(BridgeProtocolError, "framing failure")

            if bodies:
                self._queued_bodies.extend(bodies)
                return self._queued_bodies.pop(0)

    def _fatal(
        self,
        error_type: type[BridgeError],
        message: str,
    ) -> _NoReturn:
        self._close_transport()
        raise error_type(message)

    def _close_transport(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.shutdown(_socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._connection.close()
        except OSError:
            pass


class BridgeServer:
    """Accept one authenticated runner connection on loopback."""

    def __init__(self, *, session_id: str, token: str) -> None:
        self._session_id = session_id
        self._token = token
        self._listener: _socket.socket | None = None
        self._listened = False
        self._accepted = False

    def listen(self) -> tuple[str, int]:
        """Bind an ephemeral loopback listener and return its address."""
        if self._listened:
            raise BridgeError("already listening")

        listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            host, port = listener.getsockname()
        except Exception:
            listener.close()
            raise

        self._listener = listener
        self._listened = True
        return host, port

    def accept(
        self,
        timeout: float = _DEFAULT_DEADLINE_SECONDS,
    ) -> BridgeSession:
        """Accept, authenticate, and return the server's sole session."""
        if self._accepted:
            raise BridgeError("already accepted")
        if self._listener is None:
            raise BridgeError("not listening")

        end = _time.monotonic() + timeout
        remaining = end - _time.monotonic()
        if remaining <= 0:
            raise BridgeTimeoutError("hello timeout")

        try:
            self._listener.settimeout(remaining)
            connection, _ = self._listener.accept()
        except _socket.timeout:
            raise BridgeTimeoutError("hello timeout") from None

        try:
            body, decoder, queued_bodies = self._receive_hello(
                connection,
                end,
            )
            try:
                hello = _BridgeRequest.model_validate(body)
            except _ValidationError:
                raise BridgeProtocolError("invalid hello") from None

            if hello.type is not _MessageType.HELLO:
                raise BridgeProtocolError("invalid hello")
            if hello.session_id != self._session_id:
                self._reject_hello(
                    connection,
                    hello,
                    code="bridge.session_mismatch",
                    message="Hello session did not match.",
                    error_message="session mismatch",
                    end=end,
                )
            if hello.payload.get("token") != self._token:
                self._reject_hello(
                    connection,
                    hello,
                    code="bridge.token_mismatch",
                    message="Hello token did not match.",
                    error_message="token mismatch",
                    end=end,
                )

            response = _BridgeResponse(
                protocol_version=hello.protocol_version,
                session_id=hello.session_id,
                request_id=hello.request_id,
                type=hello.type,
                ok=True,
                payload={"session_id": self._session_id},
            )
            self._send_hello_response(connection, response, end)
        except BridgeError:
            self._close_connection(connection)
            raise
        except Exception:
            self._close_connection(connection)
            raise BridgeProtocolError("invalid hello") from None

        self._accepted = True
        return BridgeSession(
            connection=connection,
            session_id=self._session_id,
            protocol_version=hello.protocol_version,
            decoder=decoder,
            queued_bodies=queued_bodies,
        )

    def close(self) -> None:
        """Close the listener without affecting an accepted session."""
        listener = self._listener
        self._listener = None
        if listener is None:
            return
        try:
            listener.close()
        except OSError:
            pass

    @staticmethod
    def _receive_hello(
        connection: _socket.socket,
        end: float,
    ) -> tuple[
        dict[str, object],
        _FrameDecoder,
        list[dict[str, object]],
    ]:
        decoder = _FrameDecoder()
        while True:
            remaining = end - _time.monotonic()
            if remaining <= 0:
                raise BridgeTimeoutError("hello timeout")

            try:
                connection.settimeout(
                    min(_SOCKET_POLL_SECONDS, remaining)
                )
                chunk = connection.recv(_RECV_BYTES)
            except _socket.timeout:
                continue
            except OSError:
                raise BridgeProtocolError("invalid hello") from None

            if not chunk:
                raise BridgeProtocolError("invalid hello")

            try:
                bodies = decoder.feed(chunk)
            except Exception:
                raise BridgeProtocolError("invalid hello") from None
            if bodies:
                return bodies[0], decoder, bodies[1:]

    def _reject_hello(
        self,
        connection: _socket.socket,
        hello: _BridgeRequest,
        *,
        code: str,
        message: str,
        error_message: str,
        end: float,
    ) -> _NoReturn:
        response = _BridgeResponse(
            protocol_version=hello.protocol_version,
            session_id=hello.session_id,
            request_id=hello.request_id,
            type=hello.type,
            ok=False,
            error=_Signal(
                code=code,
                severity=_SignalSeverity.FAILURE,
                message=message,
            ),
        )
        try:
            self._send_hello_response(connection, response, end)
        except BridgeError:
            pass
        self._close_connection(connection)
        raise BridgeProtocolError(error_message)

    @staticmethod
    def _send_hello_response(
        connection: _socket.socket,
        response: _BridgeResponse,
        end: float,
    ) -> None:
        remaining = end - _time.monotonic()
        if remaining <= 0:
            raise BridgeTimeoutError("hello timeout")
        try:
            connection.settimeout(remaining)
            connection.sendall(_encode_frame(response))
        except _socket.timeout:
            raise BridgeTimeoutError("hello timeout") from None
        except OSError:
            raise BridgeProtocolError("invalid hello") from None

    @staticmethod
    def _close_connection(connection: _socket.socket) -> None:
        try:
            connection.shutdown(_socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass
