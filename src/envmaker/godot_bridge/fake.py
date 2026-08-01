"""Protocol-faithful fake Godot runner for bridge contract tests.
Tick policy (N-001): an envelope-valid simulation request consumes its tick regardless of ok — matching real Godot. BridgeSession.last_tick remains ok-only; the Task 3 runtime driver owns its outbound tick counter.
"""

from __future__ import annotations

import socket
import threading
import time

from pydantic import ValidationError

from envmaker.core.contracts import (
    DEFAULT_DEADLINE_SECONDS,
    MAX_IN_FLIGHT_BYTES,
    MAX_QUEUE_DEPTH,
    PROTOCOL_VERSION,
    SIMULATION_TYPES,
    BridgeRequest,
    BridgeResponse,
    MessageType,
)
from envmaker.core.signals import Signal, SignalSeverity
from envmaker.godot_bridge.protocol import FrameDecoder, encode_frame

__all__ = ["FakeRunner"]

_SOCKET_POLL_SECONDS = 0.1
_RECV_BYTES = 65_536


def _in_flight_exceeded(
    buffered_bytes: int,
    pending_body_bytes: int,
) -> bool:
    return buffered_bytes + pending_body_bytes > MAX_IN_FLIGHT_BYTES


class FakeRunner:
    """Serve deterministic fake-Godot responses over a real TCP connection."""

    def __init__(
        self,
        *,
        session_id: str,
        token: str,
        delay_by_type: dict[MessageType, float] | None = None,
    ) -> None:
        self._session_id = session_id
        self._token = token
        self._delay_by_type = dict(delay_by_type or {})
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._closed_reason: str | None = None
        self._last_incoming_request_id = 0
        self._last_tick_id: int | None = None

    def connect(self, host: str, port: int, timeout: float = 5.0) -> None:
        """Connect to a bridge listener and start the daemon serve thread."""
        connection = socket.create_connection((host, port), timeout=timeout)
        connection.settimeout(_SOCKET_POLL_SECONDS)
        self._stop_event.clear()
        with self._state_lock:
            self._socket = connection
            self._closed_reason = None
        self._last_incoming_request_id = 0
        self._last_tick_id = None
        thread = threading.Thread(
            target=self._serve,
            args=(connection,),
            name="envmaker-fake-runner",
            daemon=True,
        )
        with self._state_lock:
            self._thread = thread
        try:
            thread.start()
        except Exception:
            self._close_socket(connection)
            raise

    def stop(self, timeout: float = 5.0) -> None:
        """Stop serving, close the socket, and wait at most ``timeout`` seconds."""
        self._stop_event.set()
        with self._state_lock:
            connection = self._socket
            thread = self._thread
        if connection is not None:
            self._close_socket(connection)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    @property
    def closed_reason(self) -> str | None:
        """Return why the connection closed, if the runner closed it."""
        with self._state_lock:
            return self._closed_reason

    @property
    def alive(self) -> bool:
        """Return whether the serve thread is running."""
        with self._state_lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def _serve(self, connection: socket.socket) -> None:
        decoder = FrameDecoder()
        try:
            initial_batch = self._exchange_hello(connection, decoder)
            if initial_batch is None:
                return
            initial_bodies, initial_body_lengths = initial_batch

            pending: list[tuple[BridgeRequest, int]] = []
            if not self._handle_batch(
                connection,
                pending,
                initial_bodies,
                initial_body_lengths,
                decoder.buffered_bytes,
            ):
                return

            while not self._stop_event.is_set():
                try:
                    chunk = connection.recv(_RECV_BYTES)
                except socket.timeout:
                    continue
                except OSError:
                    if self._stop_event.is_set():
                        return
                    raise

                if not chunk:
                    self._set_closed_reason("client_closed")
                    return

                try:
                    bodies = decoder.feed(chunk)
                except Exception:
                    self._set_closed_reason("protocol_error: framing")
                    return

                if not self._handle_batch(
                    connection,
                    pending,
                    bodies,
                    decoder.last_batch_body_lengths,
                    decoder.buffered_bytes,
                ):
                    return
        except Exception as error:
            if not self._stop_event.is_set():
                self._set_closed_reason(
                    f"internal_error: {type(error).__name__}"
                )
        finally:
            self._close_socket(connection)

    def _exchange_hello(
        self,
        connection: socket.socket,
        decoder: FrameDecoder,
    ) -> tuple[list[dict[str, object]], tuple[int, ...]] | None:
        hello = BridgeRequest(
            protocol_version=PROTOCOL_VERSION,
            session_id=self._session_id,
            request_id=1,
            type=MessageType.HELLO,
            payload={"token": self._token},
        )
        connection.sendall(encode_frame(hello))
        deadline = time.monotonic() + DEFAULT_DEADLINE_SECONDS

        while not self._stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("hello response deadline exceeded")
            connection.settimeout(min(_SOCKET_POLL_SECONDS, remaining))
            try:
                chunk = connection.recv(_RECV_BYTES)
            except socket.timeout:
                continue
            except OSError:
                if self._stop_event.is_set():
                    return None
                raise

            if not chunk:
                self._set_closed_reason("client_closed")
                return None

            try:
                bodies = decoder.feed(chunk)
            except Exception:
                self._set_closed_reason("protocol_error: framing")
                return None
            if not bodies:
                continue

            hello_response = BridgeResponse.model_validate(bodies[0])
            if not hello_response.ok:
                assert hello_response.error is not None
                self._set_closed_reason(
                    f"hello_rejected: {hello_response.error.code}"
                )
                return None

            connection.settimeout(_SOCKET_POLL_SECONDS)
            return bodies[1:], decoder.last_batch_body_lengths[1:]

        return None

    def _handle_batch(
        self,
        connection: socket.socket,
        pending: list[tuple[BridgeRequest, int]],
        bodies: list[dict[str, object]],
        body_lengths: tuple[int, ...],
        buffered_bytes: int,
    ) -> bool:
        decoded_batch: list[BridgeRequest] = []
        for body in bodies:
            try:
                decoded_batch.append(BridgeRequest.model_validate(body))
            except ValidationError:
                self._set_closed_reason("protocol_error: invalid envelope")
                self._send_invalid_envelope(connection, body)
                return False

        running = sum(length for _, length in pending)
        decoded_with_lengths: list[tuple[BridgeRequest, int]] = []
        for request, length in zip(
            decoded_batch,
            body_lengths,
            strict=True,
        ):
            running += length
            if _in_flight_exceeded(buffered_bytes, running):
                self._set_closed_reason("protocol_error: in-flight overflow")
                self._send_error(
                    connection,
                    request,
                    code="bridge.in_flight_overflow",
                    message=(
                        "In-flight request bytes exceeded the maximum budget."
                    ),
                )
                return False
            decoded_with_lengths.append((request, length))

        pending.extend(decoded_with_lengths)
        if len(pending) > MAX_QUEUE_DEPTH:
            offending_request = pending[MAX_QUEUE_DEPTH][0]
            self._set_closed_reason("protocol_error: queue overflow")
            self._send_error(
                connection,
                offending_request,
                code="bridge.queue_overflow",
                message="Pending request queue exceeded its maximum depth.",
            )
            return False

        while pending:
            request, _ = pending.pop(0)
            if not self._process_request(connection, request):
                return False
        return True

    def _process_request(
        self,
        connection: socket.socket,
        request: BridgeRequest,
    ) -> bool:
        if request.session_id != self._session_id:
            self._set_closed_reason("protocol_error: invalid envelope")
            self._send_error(
                connection,
                request,
                code="bridge.invalid_envelope",
                message="Incoming request envelope is invalid.",
            )
            return False

        if request.request_id <= self._last_incoming_request_id:
            self._set_closed_reason("protocol_error: duplicate request id")
            self._send_error(
                connection,
                request,
                code="bridge.duplicate_request_id",
                message="Incoming request ids must be strictly increasing.",
            )
            return False
        self._last_incoming_request_id = request.request_id

        if request.type in SIMULATION_TYPES:
            assert request.tick_id is not None
            if (
                self._last_tick_id is not None
                and request.tick_id <= self._last_tick_id
            ):
                self._set_closed_reason("protocol_error: stale tick")
                self._send_error(
                    connection,
                    request,
                    code="bridge.stale_tick",
                    message="Simulation tick ids must be strictly increasing.",
                )
                return False
            self._last_tick_id = request.tick_id

        delay = self._delay_by_type.get(request.type)
        if delay is not None:
            time.sleep(delay)

        if request.type is MessageType.NAVIGATION_STATUS:
            payload = {"state": "unloaded"}
        elif request.type is MessageType.LOAD_CANDIDATE:
            if request.payload:
                self._send_error(
                    connection,
                    request,
                    code="bridge.unsupported_candidate",
                    message="Non-empty candidate payloads are not supported.",
                )
                return True
            payload = {"status": "empty_candidate_loaded"}
        elif (
            request.type in SIMULATION_TYPES
            or request.type is MessageType.HELLO
        ):
            self._send_error(
                connection,
                request,
                code="bridge.not_implemented",
                message="Request handling is not implemented.",
            )
            return True
        else:
            assert request.type is MessageType.CLOSE
            payload = {}

        response = BridgeResponse(
            protocol_version=request.protocol_version,
            session_id=request.session_id,
            request_id=request.request_id,
            tick_id=(
                request.tick_id if request.type in SIMULATION_TYPES else None
            ),
            type=request.type,
            ok=True,
            payload=payload,
        )
        connection.sendall(encode_frame(response))

        if request.type is MessageType.CLOSE:
            self._set_closed_reason("served_close")
            return False
        return True

    def _send_error(
        self,
        connection: socket.socket,
        request: BridgeRequest,
        *,
        code: str,
        message: str,
    ) -> None:
        response = BridgeResponse(
            protocol_version=request.protocol_version,
            session_id=request.session_id,
            request_id=request.request_id,
            tick_id=(
                request.tick_id if request.type in SIMULATION_TYPES else None
            ),
            type=request.type,
            ok=False,
            error=Signal(
                code=code,
                severity=SignalSeverity.FAILURE,
                message=message,
            ),
        )
        connection.sendall(encode_frame(response))

    def _send_invalid_envelope(
        self,
        connection: socket.socket,
        body: dict[str, object],
    ) -> None:
        message_type = self._valid_message_type(body.get("type"))
        request_id = self._valid_request_id(body.get("request_id"))
        session_id = body.get("session_id")
        if not isinstance(session_id, str):
            session_id = self._session_id
        tick_id = None
        if message_type in SIMULATION_TYPES:
            tick_id = self._valid_tick_id(body.get("tick_id"))

        error = Signal(
            code="bridge.invalid_envelope",
            severity=SignalSeverity.FAILURE,
            message="Incoming request envelope is invalid.",
        )
        try:
            response = BridgeResponse(
                protocol_version=PROTOCOL_VERSION,
                session_id=session_id,
                request_id=request_id,
                tick_id=tick_id,
                type=message_type,
                ok=False,
                error=error,
            )
        except ValidationError:
            response = BridgeResponse(
                protocol_version=PROTOCOL_VERSION,
                session_id=self._session_id,
                request_id=request_id,
                tick_id=tick_id,
                type=message_type,
                ok=False,
                error=error,
            )
        connection.sendall(encode_frame(response))

    @staticmethod
    def _valid_message_type(value: object) -> MessageType:
        try:
            return MessageType(value)
        except (TypeError, ValueError):
            return MessageType.HELLO

    @staticmethod
    def _valid_request_id(value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            return value
        return 1

    @staticmethod
    def _valid_tick_id(value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return 0

    def _set_closed_reason(self, reason: str) -> None:
        with self._state_lock:
            if self._closed_reason is None:
                self._closed_reason = reason

    def _close_socket(self, connection: socket.socket) -> None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        connection.close()
        with self._state_lock:
            if self._socket is connection:
                self._socket = None
