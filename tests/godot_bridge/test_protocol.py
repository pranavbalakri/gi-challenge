import pytest
from pydantic import ValidationError

from envmaker.core.contracts import (
    PROTOCOL_VERSION,
    SIMULATION_TYPES,
    BridgeRequest,
    BridgeResponse,
    MessageType,
)
from envmaker.core.signals import Signal, SignalSeverity


def _request_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": "session-1",
        "request_id": 1,
        "type": MessageType.HELLO,
    }
    data.update(overrides)
    return data


def _response_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": "session-1",
        "request_id": 1,
        "type": MessageType.HELLO,
        "ok": True,
    }
    data.update(overrides)
    return data


def test_message_type_values() -> None:
    assert [message_type.value for message_type in MessageType] == [
        "hello",
        "load_candidate",
        "navigation_status",
        "reset",
        "step",
        "snapshot",
        "render",
        "probe",
        "close",
    ]
    assert SIMULATION_TYPES == {
        MessageType.RESET,
        MessageType.STEP,
        MessageType.SNAPSHOT,
        MessageType.RENDER,
        MessageType.PROBE,
    }


def test_request_tick_rules() -> None:
    with pytest.raises(
        ValidationError,
        match="tick_id required for simulation message",
    ):
        BridgeRequest.model_validate(_request_data(type=MessageType.STEP))

    with pytest.raises(
        ValidationError,
        match="tick_id forbidden for control message",
    ):
        BridgeRequest.model_validate(
            _request_data(type=MessageType.HELLO, tick_id=0)
        )

    hello = BridgeRequest.model_validate(_request_data(type=MessageType.HELLO))
    assert hello.tick_id is None

    step = BridgeRequest.model_validate(
        _request_data(type=MessageType.STEP, tick_id=5)
    )
    assert step.tick_id == 5

    with pytest.raises(ValidationError):
        BridgeRequest.model_validate(
            _request_data(type=MessageType.STEP, tick_id=-1)
        )


def test_request_envelope_bounds() -> None:
    with pytest.raises(ValidationError):
        BridgeRequest.model_validate(_request_data(protocol_version=2))

    with pytest.raises(ValidationError):
        BridgeRequest.model_validate(_request_data(request_id=0))

    with pytest.raises(ValidationError):
        BridgeRequest.model_validate(_request_data(session_id="Bad_Session!"))

    with pytest.raises(ValidationError):
        BridgeRequest.model_validate(
            _request_data(payload={f"key-{index}": index for index in range(65)})
        )

    request = BridgeRequest.model_validate(
        _request_data(payload={f"key-{index}": index for index in range(64)})
    )
    assert len(request.payload) == 64


def test_response_error_invariant() -> None:
    with pytest.raises(
        ValidationError,
        match="error signal required when not ok",
    ):
        BridgeResponse.model_validate(_response_data(ok=False, error=None))

    error = Signal(
        code="bridge.failure",
        severity=SignalSeverity.FAILURE,
        message="Bridge request failed.",
    )
    with pytest.raises(
        ValidationError,
        match="error signal forbidden when ok",
    ):
        BridgeResponse.model_validate(_response_data(ok=True, error=error))

    response = BridgeResponse.model_validate(
        _response_data(ok=False, error=error)
    )
    assert response.error == error


from envmaker.core.contracts import MAX_CONTROL_MESSAGE_BYTES
from envmaker.godot_bridge.protocol import FRAME_HEADER_BYTES, FrameDecoder, FramingError, encode_frame


def test_frame_roundtrip() -> None:
    first = BridgeRequest.model_validate(_request_data(payload={}))
    second = BridgeRequest.model_validate(_request_data(request_id=2, payload={}))
    first_body = first.model_dump(mode="json")
    second_body = second.model_dump(mode="json")

    single_decoder = FrameDecoder()
    assert single_decoder.feed(encode_frame(first)) == [first_body]
    assert single_decoder.buffered_bytes == 0

    multi_decoder = FrameDecoder()
    assert multi_decoder.feed(encode_frame(first) + encode_frame(second)) == [
        first_body,
        second_body,
    ]
    assert multi_decoder.buffered_bytes == 0


def test_frame_split_reassembly() -> None:
    message = BridgeRequest.model_validate(_request_data(payload={}))
    frame = encode_frame(message)
    chunks = [frame[offset : offset + 3] for offset in range(0, len(frame), 3)]
    decoder = FrameDecoder()

    for chunk in chunks[:-1]:
        assert decoder.feed(chunk) == []
    assert decoder.feed(chunks[-1]) == [message.model_dump(mode="json")]
    assert decoder.buffered_bytes == 0


def test_frame_oversized_rejected() -> None:
    oversized_header = (MAX_CONTROL_MESSAGE_BYTES + 1).to_bytes(4, "big")
    decoder = FrameDecoder()

    with pytest.raises(
        FramingError,
        match="^frame exceeds control message limit$",
    ):
        decoder.feed(oversized_header)

    oversized_message = BridgeRequest.model_validate(
        _request_data(payload={"value": "x" * MAX_CONTROL_MESSAGE_BYTES})
    )
    with pytest.raises(
        FramingError,
        match="^frame exceeds control message limit$",
    ):
        encode_frame(oversized_message)


def test_frame_malformed_json() -> None:
    malformed_body = b"{not json"
    malformed_frame = len(malformed_body).to_bytes(4, "big") + malformed_body
    with pytest.raises(FramingError, match="^malformed json frame$"):
        FrameDecoder().feed(malformed_frame)

    list_body = b"[1,2]"
    list_frame = len(list_body).to_bytes(4, "big") + list_body
    with pytest.raises(FramingError, match="^frame must be a json object$"):
        FrameDecoder().feed(list_frame)


def test_frame_zero_and_partial_header() -> None:
    with pytest.raises(FramingError, match="^empty frame$"):
        FrameDecoder().feed((0).to_bytes(4, "big"))

    decoder = FrameDecoder()
    assert decoder.feed(b"\x00\x00") == []
    assert decoder.buffered_bytes == 2


def test_decoder_poisoned_after_error() -> None:
    decoder = FrameDecoder()
    with pytest.raises(FramingError, match="^empty frame$"):
        decoder.feed((0).to_bytes(4, "big"))

    valid_message = BridgeRequest.model_validate(_request_data(payload={}))
    with pytest.raises(
        FramingError,
        match="^framing decoder already failed$",
    ):
        decoder.feed(encode_frame(valid_message))


import socket
import time

from envmaker.core.contracts import MAX_QUEUE_DEPTH
from envmaker.godot_bridge.fake import FakeRunner


class _RawHarness:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.host, self.port = self._listener.getsockname()
        self._connection: socket.socket | None = None
        self._decoder = FrameDecoder()
        self._queued_bodies: list[dict[str, object]] = []

    def accept(self) -> None:
        self._listener.settimeout(5.0)
        self._connection, _ = self._listener.accept()

    def expect_hello(self, reply_ok: bool) -> BridgeRequest:
        (body,) = self._read_bodies(1, time.monotonic() + 5.0)
        hello = BridgeRequest.model_validate(body)
        assert hello.type is MessageType.HELLO
        error = None
        if not reply_ok:
            error = Signal(
                code="bridge.token_mismatch",
                severity=SignalSeverity.FAILURE,
                message="Hello token did not match.",
            )
        response = BridgeResponse(
            protocol_version=PROTOCOL_VERSION,
            session_id=hello.session_id,
            request_id=hello.request_id,
            type=hello.type,
            ok=reply_ok,
            error=error,
        )
        self.send_raw(encode_frame(response))
        return hello

    def send_request(
        self,
        *,
        request_id: int,
        message_type: MessageType,
        payload: dict[str, object] | None = None,
        tick_id: int | None = None,
    ) -> None:
        request = BridgeRequest(
            protocol_version=PROTOCOL_VERSION,
            session_id=self.session_id,
            request_id=request_id,
            tick_id=tick_id,
            type=message_type,
            payload={} if payload is None else payload,
        )
        self.send_raw(encode_frame(request))

    def send_raw(self, data: bytes) -> None:
        connection = self._connected_socket()
        sent = connection.send(data)
        assert sent == len(data)

    def read_responses(
        self,
        n: int,
        deadline: float,
    ) -> list[BridgeResponse]:
        bodies = self._read_bodies(n, time.monotonic() + deadline)
        return [BridgeResponse.model_validate(body) for body in bodies]

    def read_until_eof(self, deadline: float) -> list[dict[str, object]]:
        bodies = self._queued_bodies
        self._queued_bodies = []
        connection = self._connected_socket()
        end = time.monotonic() + deadline
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("connection did not close before deadline")
            connection.settimeout(remaining)
            try:
                chunk = connection.recv(65_536)
            except socket.timeout:
                raise TimeoutError(
                    "connection did not close before deadline"
                ) from None
            if not chunk:
                return bodies
            bodies.extend(self._decoder.feed(chunk))

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
        self._listener.close()

    def _connected_socket(self) -> socket.socket:
        assert self._connection is not None
        return self._connection

    def _read_bodies(
        self,
        n: int,
        end: float,
    ) -> list[dict[str, object]]:
        connection = self._connected_socket()
        while len(self._queued_bodies) < n:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("responses did not arrive before deadline")
            connection.settimeout(remaining)
            try:
                chunk = connection.recv(65_536)
            except socket.timeout:
                raise TimeoutError(
                    "responses did not arrive before deadline"
                ) from None
            if not chunk:
                raise EOFError("connection closed before responses arrived")
            self._queued_bodies.extend(self._decoder.feed(chunk))
        bodies = self._queued_bodies[:n]
        del self._queued_bodies[:n]
        return bodies


def test_fake_hello_flow() -> None:
    harness = _RawHarness("fake-session")
    fake = FakeRunner(session_id="fake-session", token="secret-token")
    try:
        fake.connect(harness.host, harness.port)
        harness.accept()
        hello = harness.expect_hello(reply_ok=True)
        assert hello.session_id == "fake-session"
        assert hello.payload == {"token": "secret-token"}

        harness.send_request(
            request_id=1,
            message_type=MessageType.NAVIGATION_STATUS,
        )
        (navigation_response,) = harness.read_responses(1, 2.0)
        assert navigation_response.ok
        assert navigation_response.payload == {"state": "unloaded"}

        harness.send_request(
            request_id=2,
            message_type=MessageType.LOAD_CANDIDATE,
        )
        (load_response,) = harness.read_responses(1, 2.0)
        assert load_response.ok
        assert load_response.payload == {"status": "empty_candidate_loaded"}
        assert fake.closed_reason is None
    finally:
        fake.stop()
        harness.close()
    assert not fake.alive


def test_fake_hello_rejected() -> None:
    harness = _RawHarness("fake-session")
    fake = FakeRunner(session_id="fake-session", token="secret-token")
    try:
        fake.connect(harness.host, harness.port)
        harness.accept()
        harness.expect_hello(reply_ok=False)
        assert harness.read_until_eof(2.0) == []
        assert fake.closed_reason == "hello_rejected: bridge.token_mismatch"
        fake.stop()
        assert not fake.alive
    finally:
        fake.stop()
        harness.close()


def test_fake_duplicate_request_id() -> None:
    harness = _RawHarness("fake-session")
    fake = FakeRunner(session_id="fake-session", token="secret-token")
    try:
        fake.connect(harness.host, harness.port)
        harness.accept()
        harness.expect_hello(reply_ok=True)

        harness.send_request(
            request_id=5,
            message_type=MessageType.NAVIGATION_STATUS,
        )
        (first_response,) = harness.read_responses(1, 2.0)
        assert first_response.ok

        harness.send_request(
            request_id=5,
            message_type=MessageType.NAVIGATION_STATUS,
        )
        (duplicate_response,) = harness.read_responses(1, 2.0)
        assert not duplicate_response.ok
        assert duplicate_response.error is not None
        assert duplicate_response.error.code == "bridge.duplicate_request_id"
        assert harness.read_until_eof(2.0) == []
        assert fake.closed_reason == "protocol_error: duplicate request id"
    finally:
        fake.stop()
        harness.close()


def test_fake_stale_tick() -> None:
    harness = _RawHarness("fake-session")
    fake = FakeRunner(session_id="fake-session", token="secret-token")
    try:
        fake.connect(harness.host, harness.port)
        harness.accept()
        harness.expect_hello(reply_ok=True)

        harness.send_request(
            request_id=1,
            message_type=MessageType.RESET,
            tick_id=0,
        )
        (reset_response,) = harness.read_responses(1, 2.0)
        assert not reset_response.ok
        assert reset_response.error is not None
        assert reset_response.error.code == "bridge.not_implemented"

        harness.send_request(
            request_id=2,
            message_type=MessageType.STEP,
            tick_id=1,
        )
        (step_response,) = harness.read_responses(1, 2.0)
        assert not step_response.ok
        assert step_response.error is not None
        assert step_response.error.code == "bridge.not_implemented"

        harness.send_request(
            request_id=3,
            message_type=MessageType.STEP,
            tick_id=1,
        )
        (stale_response,) = harness.read_responses(1, 2.0)
        assert not stale_response.ok
        assert stale_response.error is not None
        assert stale_response.error.code == "bridge.stale_tick"
        assert harness.read_until_eof(2.0) == []
        assert fake.closed_reason == "protocol_error: stale tick"
    finally:
        fake.stop()
        harness.close()


def test_fake_queue_overflow() -> None:
    harness = _RawHarness("fake-session")
    fake = FakeRunner(session_id="fake-session", token="secret-token")
    try:
        fake.connect(harness.host, harness.port)
        harness.accept()
        harness.expect_hello(reply_ok=True)

        frames = [
            encode_frame(
                BridgeRequest(
                    protocol_version=PROTOCOL_VERSION,
                    session_id="fake-session",
                    request_id=request_id,
                    type=MessageType.NAVIGATION_STATUS,
                )
            )
            for request_id in range(1, MAX_QUEUE_DEPTH + 2)
        ]
        harness.send_raw(b"".join(frames))

        (overflow_response,) = harness.read_responses(1, 2.0)
        assert not overflow_response.ok
        assert overflow_response.request_id == MAX_QUEUE_DEPTH + 1
        assert overflow_response.error is not None
        assert overflow_response.error.code == "bridge.queue_overflow"
        assert harness.read_until_eof(2.0) == []
        assert fake.closed_reason == "protocol_error: queue overflow"
    finally:
        fake.stop()
        harness.close()


def test_fake_feed_exception_fatal() -> None:
    harness = _RawHarness("fake-session")
    fake = FakeRunner(session_id="fake-session", token="secret-token")
    try:
        fake.connect(harness.host, harness.port)
        harness.accept()
        harness.expect_hello(reply_ok=True)

        body = ("[" * 40_000 + "]" * 40_000).encode()
        harness.send_raw(len(body).to_bytes(4, "big") + body)

        assert harness.read_until_eof(2.0) == []
        assert fake.closed_reason == "protocol_error: framing"
    finally:
        fake.stop()
        harness.close()


def test_fake_delay_hook() -> None:
    harness = _RawHarness("fake-session")
    fake = FakeRunner(
        session_id="fake-session",
        token="secret-token",
        delay_by_type={MessageType.NAVIGATION_STATUS: 0.3},
    )
    try:
        fake.connect(harness.host, harness.port)
        harness.accept()
        harness.expect_hello(reply_ok=True)

        started = time.monotonic()
        harness.send_request(
            request_id=1,
            message_type=MessageType.NAVIGATION_STATUS,
        )
        (response,) = harness.read_responses(1, 2.0)
        elapsed = time.monotonic() - started

        assert response.ok
        assert elapsed >= 0.3
    finally:
        fake.stop()
        harness.close()


from envmaker.godot_bridge.client import (
    BridgeError,
    BridgeProtocolError,
    BridgeServer,
    BridgeSession,
    BridgeTimeoutError,
)


def _start_bridge(
    *,
    session_id: str = "bridge-session",
    token: str = "secret-token",
    fake_session_id: str | None = None,
    fake_token: str | None = None,
    delay_by_type: dict[MessageType, float] | None = None,
) -> tuple[BridgeServer, FakeRunner, BridgeSession]:
    server = BridgeServer(session_id=session_id, token=token)
    host, port = server.listen()
    fake = FakeRunner(
        session_id=session_id if fake_session_id is None else fake_session_id,
        token=token if fake_token is None else fake_token,
        delay_by_type=delay_by_type,
    )
    try:
        fake.connect(host, port)
        session = server.accept()
    except Exception:
        fake.stop()
        server.close()
        raise
    return server, fake, session


def test_server_accept_and_request_flow() -> None:
    server, fake, session = _start_bridge()
    try:
        navigation = session.request(MessageType.NAVIGATION_STATUS)
        assert navigation.ok
        assert navigation.payload == {"state": "unloaded"}

        loaded = session.request(MessageType.LOAD_CANDIDATE, {})
        assert loaded.ok
        assert loaded.payload == {"status": "empty_candidate_loaded"}
        assert session.session_id == "bridge-session"
        assert session.last_tick is None
    finally:
        session.close()
        fake.stop()
        server.close()


def test_server_rejects_wrong_token() -> None:
    server = BridgeServer(session_id="bridge-session", token="secret-token")
    host, port = server.listen()
    fake = FakeRunner(session_id="bridge-session", token="wrong-token")
    try:
        fake.connect(host, port)
        with pytest.raises(BridgeProtocolError, match="^token mismatch$"):
            server.accept()
    finally:
        server.close()
        stop_deadline = time.monotonic() + 2.0
        while fake.alive and time.monotonic() < stop_deadline:
            time.sleep(0.01)
        fake.stop()
    assert fake.closed_reason == "hello_rejected: bridge.token_mismatch"


def test_server_rejects_wrong_session() -> None:
    server = BridgeServer(session_id="bridge-session", token="secret-token")
    host, port = server.listen()
    fake = FakeRunner(session_id="different-session", token="secret-token")
    try:
        fake.connect(host, port)
        with pytest.raises(BridgeProtocolError, match="^session mismatch$"):
            server.accept()
    finally:
        server.close()
        stop_deadline = time.monotonic() + 2.0
        while fake.alive and time.monotonic() < stop_deadline:
            time.sleep(0.01)
        fake.stop()
    assert fake.closed_reason == "hello_rejected: bridge.session_mismatch"


def test_session_tick_flow_updates_last_tick() -> None:
    def respond(request: BridgeRequest) -> BridgeResponse:
        return BridgeResponse(
            protocol_version=request.protocol_version,
            session_id=request.session_id,
            request_id=request.request_id,
            tick_id=request.tick_id,
            type=request.type,
            ok=True,
            payload={"tick_acknowledged": request.tick_id},
        )

    server, session = _serve_scripted_response(respond)
    try:
        step = session.request(MessageType.STEP, tick_id=3)
        assert step.ok
        assert step.payload == {"tick_acknowledged": 3}
        assert session.last_tick == 3
    finally:
        session.close()
        server.close()


def test_session_returns_error_response() -> None:
    server, fake, session = _start_bridge()
    try:
        first = session.request(MessageType.STEP, tick_id=0)
        assert not first.ok
        assert first.error is not None
        assert first.error.code == "bridge.not_implemented"
        assert session.last_tick is None

        follow_up = session.request(MessageType.NAVIGATION_STATUS)
        assert follow_up.ok

        stale = session.request(MessageType.STEP, tick_id=0)
        assert not stale.ok
        assert stale.error is not None
        assert stale.error.code == "bridge.stale_tick"

        with pytest.raises(BridgeProtocolError):
            session.request(MessageType.NAVIGATION_STATUS)
    finally:
        session.close()
        fake.stop()
        server.close()


def test_session_timeout() -> None:
    server, fake, session = _start_bridge(
        delay_by_type={MessageType.SNAPSHOT: 1.0}
    )
    try:
        assert not session.request(MessageType.RESET, tick_id=0).ok
        with pytest.raises(
            BridgeTimeoutError,
            match="^request deadline exceeded$",
        ):
            session.request(MessageType.SNAPSHOT, tick_id=1, deadline=0.2)
        assert session.closed
        with pytest.raises(BridgeError, match="^session closed$"):
            session.request(MessageType.NAVIGATION_STATUS)
    finally:
        session.close()
        fake.stop()
        server.close()


def test_session_clean_close() -> None:
    server, fake, session = _start_bridge()
    try:
        session.close()
        fake.stop()
        assert fake.closed_reason == "served_close"
        session.close()
        with pytest.raises(BridgeError, match="^session closed$"):
            session.request(MessageType.NAVIGATION_STATUS)
    finally:
        session.close()
        fake.stop()
        server.close()


def test_accept_timeout_and_single_use() -> None:
    idle_server = BridgeServer(
        session_id="idle-session",
        token="secret-token",
    )
    idle_server.listen()
    try:
        with pytest.raises(BridgeTimeoutError, match="^hello timeout$"):
            idle_server.accept(timeout=0.2)
    finally:
        idle_server.close()

    server, fake, session = _start_bridge()
    try:
        with pytest.raises(BridgeError, match="^already accepted$"):
            server.accept()
    finally:
        session.close()
        fake.stop()
        server.close()


def test_decoder_last_batch_body_lengths() -> None:
    first = BridgeRequest(
        protocol_version=PROTOCOL_VERSION,
        session_id="session-1",
        request_id=1,
        type=MessageType.NAVIGATION_STATUS,
    )
    second = BridgeRequest(
        protocol_version=PROTOCOL_VERSION,
        session_id="session-1",
        request_id=2,
        type=MessageType.NAVIGATION_STATUS,
        payload={"padding": "x" * 100},
    )
    first_frame = encode_frame(first)
    second_frame = encode_frame(second)
    first_length = len(first_frame) - FRAME_HEADER_BYTES
    second_length = len(second_frame) - FRAME_HEADER_BYTES

    decoder = FrameDecoder()
    assert decoder.last_batch_body_lengths == ()

    assert len(decoder.feed(first_frame)) == 1
    assert decoder.last_batch_body_lengths == (first_length,)

    assert decoder.feed(second_frame[:10]) == []
    assert decoder.last_batch_body_lengths == ()
    assert len(decoder.feed(second_frame[10:])) == 1
    assert decoder.last_batch_body_lengths == (second_length,)

    both = FrameDecoder()
    assert len(both.feed(first_frame + second_frame)) == 2
    assert both.last_batch_body_lengths == (first_length, second_length)

    failing = FrameDecoder()
    failing.feed(first_frame)
    with pytest.raises(FramingError):
        failing.feed(b"\x00\x00\x00\x00")
    assert failing.last_batch_body_lengths == ()


def test_in_flight_accounting_boundary() -> None:
    from envmaker.core.contracts import MAX_IN_FLIGHT_BYTES
    from envmaker.godot_bridge.fake import _in_flight_exceeded

    assert not _in_flight_exceeded(0, MAX_IN_FLIGHT_BYTES)
    assert _in_flight_exceeded(0, MAX_IN_FLIGHT_BYTES + 1)
    assert _in_flight_exceeded(1, MAX_IN_FLIGHT_BYTES)


def test_fake_in_flight_overflow_batch() -> None:
    from envmaker.core.contracts import MAX_IN_FLIGHT_BYTES

    def body_for(request_id: int) -> dict[str, object]:
        return BridgeRequest(
            protocol_version=PROTOCOL_VERSION,
            session_id="fake-session",
            request_id=request_id,
            type=MessageType.NAVIGATION_STATUS,
        ).model_dump(mode="json")

    def read_responses(
        source: socket.socket, expected: int
    ) -> list[BridgeResponse]:
        source.settimeout(5.0)
        decoder = FrameDecoder()
        responses: list[BridgeResponse] = []
        while len(responses) < expected:
            for body in decoder.feed(source.recv(65536)):
                responses.append(BridgeResponse.model_validate(body))
        return responses

    half = MAX_IN_FLIGHT_BYTES // 2

    runner = FakeRunner(session_id="fake-session", token="secret-token")
    left, right = socket.socketpair()
    try:
        pending: list[tuple[BridgeRequest, int]] = []
        assert (
            runner._handle_batch(
                right, pending, [body_for(1), body_for(2)], (half, half), 0
            )
            is True
        )
        assert pending == []
        assert runner.closed_reason is None
        served = read_responses(left, 2)
        assert [response.request_id for response in served] == [1, 2]
        assert all(response.ok for response in served)
    finally:
        left.close()
        right.close()

    runner = FakeRunner(session_id="fake-session", token="secret-token")
    left, right = socket.socketpair()
    try:
        pending = []
        assert (
            runner._handle_batch(
                right,
                pending,
                [body_for(1), body_for(2), body_for(3)],
                (half, half, 1),
                0,
            )
            is False
        )
        assert runner.closed_reason == "protocol_error: in-flight overflow"
        (rejection,) = read_responses(left, 1)
        assert rejection.ok is False
        assert rejection.request_id == 3
        assert rejection.error is not None
        assert rejection.error.code == "bridge.in_flight_overflow"
        left.settimeout(0.5)
        with pytest.raises(socket.timeout):
            left.recv(65536)
    finally:
        left.close()
        right.close()

    runner = FakeRunner(session_id="fake-session", token="secret-token")
    left, right = socket.socketpair()
    try:
        assert (
            runner._handle_batch(
                right, [], [body_for(1)], (MAX_IN_FLIGHT_BYTES,), 1
            )
            is False
        )
        assert runner.closed_reason == "protocol_error: in-flight overflow"
        (rejection,) = read_responses(left, 1)
        assert rejection.ok is False
        assert rejection.request_id == 1
        assert rejection.error is not None
        assert rejection.error.code == "bridge.in_flight_overflow"
    finally:
        left.close()
        right.close()


def test_session_await_bytes_guard() -> None:
    from envmaker.core.contracts import MAX_IN_FLIGHT_BYTES
    from envmaker.godot_bridge.client import _await_bytes_exceeded

    assert not _await_bytes_exceeded(MAX_IN_FLIGHT_BYTES)
    assert _await_bytes_exceeded(MAX_IN_FLIGHT_BYTES + 1)

    server, fake, session = _start_bridge()
    try:
        first = session.request(MessageType.NAVIGATION_STATUS)
        second = session.request(MessageType.NAVIGATION_STATUS)
        assert first.ok
        assert second.ok
    finally:
        session.close()
        fake.stop()
        server.close()


import threading


def _serve_scripted_response(
    build_response: "Callable[[BridgeRequest], BridgeResponse]",
) -> tuple[BridgeServer, BridgeSession]:
    server = BridgeServer(session_id="bridge-session", token="secret-token")
    host, port = server.listen()

    def run() -> None:
        connection = socket.create_connection((host, port), timeout=30.0)
        try:
            connection.settimeout(30.0)
            hello = BridgeRequest(
                protocol_version=PROTOCOL_VERSION,
                session_id="bridge-session",
                request_id=1,
                type=MessageType.HELLO,
                payload={"token": "secret-token"},
            )
            connection.sendall(encode_frame(hello))
            decoder = FrameDecoder()
            bodies: list[dict[str, object]] = []
            while len(bodies) < 2:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                bodies.extend(decoder.feed(chunk))
            request = BridgeRequest.model_validate(bodies[1])
            connection.sendall(encode_frame(build_response(request)))
            connection.recv(65536)
        except OSError:
            pass
        finally:
            connection.close()

    threading.Thread(target=run, daemon=True).start()
    session = server.accept()
    return server, session


def test_session_rejects_mismatched_response_request_id() -> None:
    def respond(request: BridgeRequest) -> BridgeResponse:
        return BridgeResponse(
            protocol_version=request.protocol_version,
            session_id=request.session_id,
            request_id=request.request_id + 1,
            type=request.type,
            ok=True,
            payload={},
        )

    server, session = _serve_scripted_response(respond)
    try:
        with pytest.raises(
            BridgeProtocolError, match="^response correlation mismatch$"
        ):
            session.request(MessageType.NAVIGATION_STATUS)
        assert session.closed
    finally:
        server.close()


def test_session_rejects_mismatched_response_session() -> None:
    def respond(request: BridgeRequest) -> BridgeResponse:
        return BridgeResponse(
            protocol_version=request.protocol_version,
            session_id="other-session",
            request_id=request.request_id,
            type=request.type,
            ok=True,
            payload={},
        )

    server, session = _serve_scripted_response(respond)
    try:
        with pytest.raises(BridgeProtocolError, match="^session mismatch$"):
            session.request(MessageType.NAVIGATION_STATUS)
        assert session.closed
    finally:
        server.close()


def test_session_rejects_mismatched_response_type() -> None:
    def respond(request: BridgeRequest) -> BridgeResponse:
        return BridgeResponse(
            protocol_version=request.protocol_version,
            session_id=request.session_id,
            request_id=request.request_id,
            type=MessageType.HELLO,
            ok=True,
            payload={},
        )

    server, session = _serve_scripted_response(respond)
    try:
        with pytest.raises(
            BridgeProtocolError, match="^response correlation mismatch$"
        ):
            session.request(MessageType.NAVIGATION_STATUS)
        assert session.closed
    finally:
        server.close()


def test_session_rejects_mismatched_response_tick() -> None:
    def respond(request: BridgeRequest) -> BridgeResponse:
        assert request.tick_id is not None
        return BridgeResponse(
            protocol_version=request.protocol_version,
            session_id=request.session_id,
            request_id=request.request_id,
            tick_id=request.tick_id + 1,
            type=request.type,
            ok=True,
            payload={},
        )

    server, session = _serve_scripted_response(respond)
    try:
        with pytest.raises(
            BridgeProtocolError, match="^response correlation mismatch$"
        ):
            session.request(MessageType.STEP, tick_id=1)
        assert session.closed
        assert session.last_tick is None
    finally:
        server.close()


def test_fake_rejects_nonempty_candidate() -> None:
    server, fake, session = _start_bridge()
    try:
        unsupported = session.request(
            MessageType.LOAD_CANDIDATE, {"unexpected": 1}
        )
        assert not unsupported.ok
        assert unsupported.error is not None
        assert unsupported.error.code == "bridge.unsupported_candidate"

        navigation = session.request(MessageType.NAVIGATION_STATUS)
        assert navigation.ok
        assert fake.closed_reason is None
    finally:
        session.close()
        fake.stop()
        server.close()


def test_fake_post_handshake_hello_not_implemented() -> None:
    harness = _RawHarness("fake-session")
    fake = FakeRunner(session_id="fake-session", token="secret-token")
    try:
        fake.connect(harness.host, harness.port)
        harness.accept()
        harness.expect_hello(reply_ok=True)

        harness.send_request(request_id=1, message_type=MessageType.HELLO)
        (hello_response,) = harness.read_responses(1, 2.0)
        assert not hello_response.ok
        assert hello_response.error is not None
        assert hello_response.error.code == "bridge.not_implemented"
        assert fake.closed_reason is None
    finally:
        fake.stop()
        harness.close()


def test_fake_rejects_session_mismatch_request() -> None:
    harness = _RawHarness("fake-session")
    fake = FakeRunner(session_id="fake-session", token="secret-token")
    try:
        fake.connect(harness.host, harness.port)
        harness.accept()
        harness.expect_hello(reply_ok=True)

        mismatched = BridgeRequest(
            protocol_version=PROTOCOL_VERSION,
            session_id="other-session",
            request_id=1,
            type=MessageType.NAVIGATION_STATUS,
        )
        harness.send_raw(encode_frame(mismatched))
        (rejection,) = harness.read_responses(1, 2.0)
        assert not rejection.ok
        assert rejection.error is not None
        assert rejection.error.code == "bridge.invalid_envelope"
        assert rejection.session_id == "other-session"
        assert harness.read_until_eof(2.0) == []
        assert fake.closed_reason == "protocol_error: invalid envelope"
    finally:
        fake.stop()
        harness.close()
