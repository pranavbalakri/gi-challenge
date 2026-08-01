extends RefCounted

const MAX_CONTROL_MESSAGE_BYTES := 1048576
const MAX_IN_FLIGHT_BYTES := 8388608


static func encode_frame(message: Dictionary) -> PackedByteArray:
	var body := JSON.stringify(message).to_utf8_buffer()
	if body.size() > MAX_CONTROL_MESSAGE_BYTES:
		return PackedByteArray()

	var body_length := body.size()
	var frame := PackedByteArray()
	frame.resize(body_length + 4)
	frame[0] = (body_length >> 24) & 0xff
	frame[1] = (body_length >> 16) & 0xff
	frame[2] = (body_length >> 8) & 0xff
	frame[3] = body_length & 0xff
	for index: int in range(body_length):
		frame[index + 4] = body[index]
	return frame


static func in_flight_exceeded(buffered_bytes: int, pending_body_bytes: int) -> bool:
	return buffered_bytes + pending_body_bytes > MAX_IN_FLIGHT_BYTES


class FrameDecoder:
	var _buffer := PackedByteArray()
	var _failed := false

	func feed(chunk: PackedByteArray) -> Dictionary:
		if _failed:
			return {"ok": false, "error": "framing decoder already failed"}

		_buffer.append_array(chunk)
		var bodies: Array = []
		var body_lengths: Array = []
		while _buffer.size() >= 4:
			var body_length: int = (
				(int(_buffer[0]) << 24)
				| (int(_buffer[1]) << 16)
				| (int(_buffer[2]) << 8)
				| int(_buffer[3])
			)
			if body_length == 0:
				return _fail("empty frame")
			if body_length > MAX_CONTROL_MESSAGE_BYTES:
				return _fail("frame exceeds control message limit")

			var frame_end := body_length + 4
			if _buffer.size() < frame_end:
				break

			var body_bytes := _buffer.slice(4, frame_end)
			_buffer = _buffer.slice(frame_end)
			var body_text := body_bytes.get_string_from_utf8()
			if body_text.to_utf8_buffer() != body_bytes:
				return _fail("malformed json frame")

			var parser := JSON.new()
			if parser.parse(body_text) != OK:
				return _fail("malformed json frame")
			if typeof(parser.data) != TYPE_DICTIONARY:
				return _fail("frame must be a json object")
			bodies.append(parser.data)
			body_lengths.append(body_length)

		return {"ok": true, "bodies": bodies, "body_lengths": body_lengths}

	func buffered_bytes() -> int:
		return _buffer.size()

	func _fail(message: String) -> Dictionary:
		_failed = true
		return {"ok": false, "error": message}
