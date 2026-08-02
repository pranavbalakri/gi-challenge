extends Node

const Protocol := preload("res://bridge/protocol.gd")
const Materializer := preload("res://scene/materializer.gd")
const NavRuntime := preload("res://scene/nav_runtime.gd")
const AgentController := preload("res://scene/agent_controller.gd")
const CameraRig := preload("res://scene/camera_rig.gd")

const MAX_QUEUE_DEPTH := 64
const CONNECT_TIMEOUT_MSEC := 10_000
const HELLO_TIMEOUT_MSEC := 10_000
const AGENT_SPEED := 6.0
const KNOWN_TYPES := [
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
const SIMULATION_TYPES := ["reset", "step", "snapshot", "render", "probe"]
const REQUEST_KEYS := [
	"protocol_version",
	"session_id",
	"request_id",
	"tick_id",
	"type",
	"payload",
]

var _peer := StreamPeerTCP.new()
var _decoder := Protocol.FrameDecoder.new()
var _pending: Array[Dictionary] = []
var _pending_lengths: Array = []
var _pending_body_bytes := 0
var _session_id := ""
var _serving := false
var _quitting := false
var _last_incoming_request_id := 0
var _last_tick_id := 0
var _has_last_tick := false
var _run_root := ""
var _world: Node3D = null
var _nav: Node3D = null
var _agent: CharacterBody3D = null
var _camera_rig: Node3D = null
var _spawn := Vector3.ZERO
var _candidate := {}
var _candidate_loaded := false


func _ready() -> void:
	var port_text := OS.get_environment("ENVMAKER_BRIDGE_PORT")
	if port_text.is_empty():
		print("[bridge] idle (no ENVMAKER_BRIDGE_PORT)")
		return

	var host := OS.get_environment("ENVMAKER_BRIDGE_HOST")
	_session_id = OS.get_environment("ENVMAKER_BRIDGE_SESSION")
	var token := OS.get_environment("ENVMAKER_BRIDGE_TOKEN")
	_run_root = OS.get_environment("ENVMAKER_BRIDGE_RUN_ROOT")
	var connect_error := _peer.connect_to_host(host, port_text.to_int())
	if connect_error != OK:
		_quit_with_code(2)
		return

	var connected: bool = await _wait_for_connection()
	if not connected:
		_quit_with_code(2)
		return
	_peer.set_no_delay(true)

	var hello := {
		"protocol_version": 1,
		"session_id": _session_id,
		"request_id": 1,
		"tick_id": null,
		"type": "hello",
		"payload": {"token": token},
	}
	if not _send(hello):
		_quit_with_code(2)
		return

	var hello_result: Dictionary = await _wait_for_hello_response()
	if not hello_result["received"]:
		if not _quitting:
			_quit_with_code(2)
		return
	var hello_response: Dictionary = hello_result["response"]
	var hello_protocol: Variant = _integer_at_least(
		hello_response.get("protocol_version"), 1
	)
	var hello_request_id: Variant = _integer_at_least(
		hello_response.get("request_id"), 1
	)
	if (
		typeof(hello_response.get("ok")) != TYPE_BOOL
		or not bool(hello_response["ok"])
		or hello_protocol == null
		or int(hello_protocol) != 1
		or typeof(hello_response.get("session_id")) != TYPE_STRING
		or hello_response["session_id"] != _session_id
		or hello_request_id == null
		or int(hello_request_id) != 1
		or typeof(hello_response.get("type")) != TYPE_STRING
		or hello_response["type"] != "hello"
		or hello_response.get("tick_id") != null
	):
		_quit_with_code(3)
		return

	var remaining_bodies: Array = hello_result["remaining"]
	var remaining_lengths: Array = hello_result["remaining_lengths"]
	var running := _pending_body_bytes
	for index: int in range(remaining_bodies.size()):
		running += int(remaining_lengths[index])
		if Protocol.in_flight_exceeded(_decoder.buffered_bytes(), running):
			_send_error_for_body(
				remaining_bodies[index],
				"bridge.in_flight_overflow",
				"In-flight request bytes exceeded the maximum budget.",
			)
			_peer.poll()
			_quit_with_code(8)
			return
	for index: int in range(remaining_bodies.size()):
		_pending.append(remaining_bodies[index])
		_pending_lengths.append(remaining_lengths[index])
	_pending_body_bytes = running
	_serving = true


func _process(_delta: float) -> void:
	if not _serving or _quitting:
		return

	_peer.poll()
	if _peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
		_quit_with_code(7)
		return

	var read_result := _read_available()
	if not read_result["ok"]:
		_quit_with_code(4)
		return
	var new_bodies: Array = read_result["bodies"]
	var new_lengths: Array = read_result["body_lengths"]
	var running := _pending_body_bytes
	for index: int in range(new_bodies.size()):
		running += int(new_lengths[index])
		if Protocol.in_flight_exceeded(_decoder.buffered_bytes(), running):
			_send_error_for_body(
				new_bodies[index],
				"bridge.in_flight_overflow",
				"In-flight request bytes exceeded the maximum budget.",
			)
			_peer.poll()
			_quit_with_code(8)
			return
	for index: int in range(new_bodies.size()):
		_pending.append(new_bodies[index])
		_pending_lengths.append(new_lengths[index])
	_pending_body_bytes = running

	if _pending.size() > MAX_QUEUE_DEPTH:
		var offending_body := _pending[MAX_QUEUE_DEPTH]
		_send_error_for_body(
			offending_body,
			"bridge.queue_overflow",
			"Pending request queue exceeded its maximum depth.",
		)
		_peer.poll()
		_quit_with_code(5)
		return

	while not _pending.is_empty() and _serving and not _quitting:
		var body: Dictionary = _pending.pop_front()
		_pending_body_bytes -= int(_pending_lengths.pop_front())
		if not _handle_request(body):
			return


func _wait_for_connection() -> bool:
	var deadline := Time.get_ticks_msec() + CONNECT_TIMEOUT_MSEC
	while Time.get_ticks_msec() < deadline:
		_peer.poll()
		var status := _peer.get_status()
		if status == StreamPeerTCP.STATUS_CONNECTED:
			return true
		if (
			status == StreamPeerTCP.STATUS_ERROR
			or status == StreamPeerTCP.STATUS_NONE
		):
			return false
		await get_tree().process_frame
	return false


func _wait_for_hello_response() -> Dictionary:
	var deadline := Time.get_ticks_msec() + HELLO_TIMEOUT_MSEC
	while Time.get_ticks_msec() < deadline:
		_peer.poll()
		if _peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			return {"received": false}

		var read_result := _read_available()
		if not read_result["ok"]:
			_quit_with_code(4)
			return {"received": false}
		var bodies: Array = read_result["bodies"]
		var lengths: Array = read_result["body_lengths"]
		if not bodies.is_empty():
			var response: Dictionary = bodies.pop_front()
			if not lengths.is_empty():
				lengths.pop_front()
			return {
				"received": true,
				"response": response,
				"remaining": bodies,
				"remaining_lengths": lengths,
			}
		await get_tree().process_frame
	return {"received": false}


func _read_available() -> Dictionary:
	var bodies: Array[Dictionary] = []
	var body_lengths: Array = []
	while _peer.get_available_bytes() > 0:
		var received := _peer.get_data(_peer.get_available_bytes())
		if received[0] != OK:
			return {"ok": false, "error": "stream read failed"}
		var decoded: Dictionary = _decoder.feed(received[1])
		if not decoded["ok"]:
			return decoded
		for body: Dictionary in decoded["bodies"]:
			bodies.append(body)
		for length: int in decoded["body_lengths"]:
			body_lengths.append(length)
	return {"ok": true, "bodies": bodies, "body_lengths": body_lengths}


func _handle_request(body: Dictionary) -> bool:
	if not _normalize_envelope(body):
		_send_error_for_body(
			body,
			"bridge.invalid_envelope",
			"Incoming request envelope is invalid.",
		)
		_peer.poll()
		_quit_with_code(6)
		return false

	var request_id: int = body["request_id"]
	if request_id <= _last_incoming_request_id:
		_send_error(
			body,
			"bridge.duplicate_request_id",
			"Incoming request ids must be strictly increasing.",
		)
		_peer.poll()
		_quit_with_code(6)
		return false
	_last_incoming_request_id = request_id

	var message_type: String = body["type"]
	if _is_simulation_type(message_type):
		var tick_id: int = body["tick_id"]
		if _has_last_tick and tick_id <= _last_tick_id:
			_send_error(
				body,
				"bridge.stale_tick",
				"Simulation tick ids must be strictly increasing.",
			)
			_peer.poll()
			_quit_with_code(6)
			return false
		_last_tick_id = tick_id
		_has_last_tick = true

	match message_type:
		"navigation_status":
			var state: String = _nav.status() if _nav != null else "unloaded"
			_send_success(body, {"state": state})
		"load_candidate":
			_handle_load_candidate(body)
		"reset":
			_handle_reset(body)
		"snapshot":
			_handle_snapshot(body)
		"render":
			_respond_render(body)
		"probe":
			_respond_probe(body)
		"step", "hello":
			_send_error(
				body,
				"bridge.not_implemented",
				"Request handling is not implemented.",
			)
		"close":
			_send_success(body, {})
			_peer.poll()
			_quit_with_code(0)
			return false
	return true


func _handle_load_candidate(body: Dictionary) -> void:
	var payload: Dictionary = body["payload"]
	if payload.is_empty():
		_send_success(body, {"status": "empty_candidate_loaded"})
		return

	var canon_value: Variant = _integer_at_least(payload.get("canon"), 1)
	var candidate_value: Variant = payload.get("payload")
	if (
		canon_value == null
		or int(canon_value) != 1
		or typeof(candidate_value) != TYPE_DICTIONARY
	):
		_send_error(
			body,
			"bridge.unsupported_candidate",
			"Non-empty candidate payloads are not supported.",
		)
		return

	_teardown_world()
	_world = Node3D.new()
	_world.name = "world"
	add_child(_world)
	var candidate: Dictionary = candidate_value
	var scene_value: Variant = candidate.get("scene")
	if typeof(scene_value) != TYPE_DICTIONARY:
		_teardown_world()
		_send_error(
			body,
			"bridge.invalid_candidate",
			"invalid candidate payload",
		)
		return
	var scene: Dictionary = scene_value
	var nodes_value: Variant = scene.get("nodes")
	var controller_semantic_id_value: Variant = scene.get("controller_semantic_id")
	var camera_value: Variant = scene.get("camera")
	if (
		typeof(nodes_value) != TYPE_ARRAY
		or typeof(controller_semantic_id_value) != TYPE_STRING
		or typeof(camera_value) != TYPE_DICTIONARY
	):
		_teardown_world()
		_send_error(
			body,
			"bridge.invalid_candidate",
			"invalid candidate payload",
		)
		return
	var nodes: Array = nodes_value
	var controller_semantic_id: String = controller_semantic_id_value
	var camera: Dictionary = camera_value
	var follow_semantic_id_value: Variant = camera.get("follow_semantic_id")
	var orthographic_size_value: Variant = camera.get("orthographic_size")
	if (
		typeof(follow_semantic_id_value) != TYPE_STRING
		or (
			typeof(orthographic_size_value) != TYPE_INT
			and typeof(orthographic_size_value) != TYPE_FLOAT
		)
	):
		_teardown_world()
		_send_error(
			body,
			"bridge.invalid_candidate",
			"invalid candidate payload",
		)
		return
	var follow_semantic_id: String = follow_semantic_id_value

	var result: Dictionary = Materializer.materialize(candidate, _world)
	if not result["ok"]:
		_teardown_world()
		_send_error(
			body,
			"bridge.invalid_candidate",
			str(result["error"]),
		)
		return

	var spawn_value: Variant = _find_node_origin(candidate, controller_semantic_id)
	if spawn_value == null:
		_teardown_world()
		_send_error(
			body,
			"bridge.invalid_candidate",
			"controller node missing",
		)
		return
	_spawn = spawn_value
	var camera_origin_value: Variant = _find_node_origin(candidate, follow_semantic_id)
	if camera_origin_value == null:
		_teardown_world()
		_send_error(
			body,
			"bridge.invalid_candidate",
			"camera follow node missing",
		)
		return

	_nav = NavRuntime.new()
	_world.add_child(_nav)
	_nav.setup(0.5)
	_nav.bake(15.0, _spawn, _spawn)
	_agent = AgentController.new()
	_world.add_child(_agent)
	_agent.global_position = _spawn + Vector3(0.0, 1.0, 0.0)
	_agent.setup(_nav.map_rid())
	_camera_rig = CameraRig.new()
	_world.add_child(_camera_rig)
	_camera_rig.setup(float(orthographic_size_value))
	_camera_rig.frame_isometric(_spawn)
	_camera_rig.set_follow_target(_agent)
	_candidate = candidate
	_candidate_loaded = true
	_send_success(
		body,
		{
			"status": "candidate_loaded",
			"nodes": nodes.size(),
			"spawn": _vec3_dict(_spawn),
		},
	)


func _handle_reset(body: Dictionary) -> void:
	if not _candidate_loaded:
		_send_error(
			body,
			"bridge.no_candidate",
			"No candidate is loaded.",
		)
		return
	_agent.global_position = _spawn + Vector3(0.0, 1.0, 0.0)
	_agent.velocity = Vector3.ZERO
	_send_success(body, {"status": "reset"})


func _handle_snapshot(body: Dictionary) -> void:
	if not _candidate_loaded:
		_send_error(
			body,
			"bridge.no_candidate",
			"No candidate is loaded.",
		)
		return
	_send_success(
		body,
		{
			"tick_id": body["tick_id"],
			"agent_transform": {
				"origin": _vec3_dict(_agent.global_position),
				"basis_x": _vec3_dict(_agent.transform.basis.x),
				"basis_y": _vec3_dict(_agent.transform.basis.y),
				"basis_z": _vec3_dict(_agent.transform.basis.z),
			},
			"agent_velocity": _vec3_dict(_agent.velocity),
			"grounded": _agent.is_on_floor(),
			"current_nav_region": "",
			"contacts": [],
			"visible_fade_groups": [],
			"faded_groups": [],
			"events": [],
		},
	)


func _vec3_dict(value: Vector3) -> Dictionary:
	return {"x": value.x, "y": value.y, "z": value.z}


func _find_node_origin(candidate: Dictionary, semantic_id: String) -> Variant:
	var scene_value: Variant = candidate.get("scene")
	if typeof(scene_value) != TYPE_DICTIONARY:
		return null
	var scene: Dictionary = scene_value
	var nodes_value: Variant = scene.get("nodes")
	if typeof(nodes_value) != TYPE_ARRAY:
		return null
	var nodes: Array = nodes_value
	for node_value: Variant in nodes:
		if typeof(node_value) != TYPE_DICTIONARY:
			continue
		var node: Dictionary = node_value
		var node_semantic_id: Variant = node.get("semantic_id")
		if (
			typeof(node_semantic_id) != TYPE_STRING
			or node_semantic_id != semantic_id
		):
			continue
		var transform_value: Variant = node.get("transform")
		if typeof(transform_value) != TYPE_DICTIONARY:
			return null
		var transform_value_dict: Dictionary = transform_value
		var origin_value: Variant = transform_value_dict.get("origin")
		if typeof(origin_value) != TYPE_DICTIONARY:
			return null
		var origin: Dictionary = origin_value
		var x_value: Variant = origin.get("x")
		var y_value: Variant = origin.get("y")
		var z_value: Variant = origin.get("z")
		if (
			(
				typeof(x_value) != TYPE_INT
				and typeof(x_value) != TYPE_FLOAT
			)
			or (
				typeof(y_value) != TYPE_INT
				and typeof(y_value) != TYPE_FLOAT
			)
			or (
				typeof(z_value) != TYPE_INT
				and typeof(z_value) != TYPE_FLOAT
			)
		):
			return null
		return Vector3(float(x_value), float(y_value), float(z_value))
	return null


func _teardown_world() -> void:
	if _world != null:
		remove_child(_world)
		_world.free()
	_world = null
	_nav = null
	_agent = null
	_camera_rig = null
	_spawn = Vector3.ZERO
	_candidate = {}
	_candidate_loaded = false


func _normalize_envelope(body: Dictionary) -> bool:
	if body.size() != REQUEST_KEYS.size():
		return false
	for key: String in REQUEST_KEYS:
		if not body.has(key):
			return false

	var protocol_version: Variant = _integer_at_least(body["protocol_version"], 1)
	if protocol_version == null or int(protocol_version) != 1:
		return false
	body["protocol_version"] = 1

	if (
		typeof(body["session_id"]) != TYPE_STRING
		or body["session_id"] != _session_id
	):
		return false

	var request_id: Variant = _integer_at_least(body["request_id"], 1)
	if request_id == null:
		return false
	body["request_id"] = int(request_id)

	if typeof(body["type"]) != TYPE_STRING:
		return false
	var message_type: String = body["type"]
	if not KNOWN_TYPES.has(message_type):
		return false

	if typeof(body["payload"]) != TYPE_DICTIONARY:
		return false
	var payload: Dictionary = body["payload"]
	if payload.size() > 64:
		return false
	for key: Variant in payload:
		if typeof(key) != TYPE_STRING:
			return false
		var payload_key: String = key
		if payload_key.is_empty() or payload_key.length() > 64:
			return false

	if _is_simulation_type(message_type):
		var tick_id: Variant = _integer_at_least(body["tick_id"], 0)
		if tick_id == null:
			return false
		body["tick_id"] = int(tick_id)
	elif body["tick_id"] != null:
		return false
	return true


func _integer_at_least(value: Variant, minimum: int) -> Variant:
	var integer: int
	if typeof(value) == TYPE_INT:
		integer = value
	elif typeof(value) == TYPE_FLOAT:
		var number: float = value
		if not is_finite(number) or number != floor(number):
			return null
		integer = int(number)
	else:
		return null
	if integer < minimum:
		return null
	return integer


func _send_success(request: Dictionary, payload: Dictionary) -> void:
	_send(
		{
			"protocol_version": 1,
			"session_id": request["session_id"],
			"request_id": request["request_id"],
			"tick_id": (
				request["tick_id"]
				if _is_simulation_type(request["type"])
				else null
			),
			"type": request["type"],
			"ok": true,
			"payload": payload,
			"error": null,
		}
	)


func _send_error(request: Dictionary, code: String, message: String) -> void:
	_send(
		{
			"protocol_version": 1,
			"session_id": request["session_id"],
			"request_id": request["request_id"],
			"tick_id": (
				request["tick_id"]
				if _is_simulation_type(request["type"])
				else null
			),
			"type": request["type"],
			"ok": false,
			"payload": {},
			"error": {
				"code": code,
				"severity": "failure",
				"message": message,
			},
		}
	)


func _send_error_for_body(body: Dictionary, code: String, message: String) -> void:
	var message_type := _safe_message_type(body.get("type"))
	var request_id: Variant = _integer_at_least(body.get("request_id"), 1)
	if request_id == null:
		request_id = 1
	var session_id := _session_id
	if (
		typeof(body.get("session_id")) == TYPE_STRING
		and _valid_session_id(body["session_id"])
	):
		session_id = body["session_id"]
	var tick_id: Variant = null
	if _is_simulation_type(message_type):
		tick_id = _integer_at_least(body.get("tick_id"), 0)
		if tick_id == null:
			tick_id = 0

	_send(
		{
			"protocol_version": 1,
			"session_id": session_id,
			"request_id": int(request_id),
			"tick_id": tick_id,
			"type": message_type,
			"ok": false,
			"payload": {},
			"error": {
				"code": code,
				"severity": "failure",
				"message": message,
			},
		}
	)


func _send(message: Dictionary) -> bool:
	var frame: PackedByteArray = Protocol.encode_frame(message)
	if frame.is_empty():
		return false
	return _peer.put_data(frame) == OK


func _safe_message_type(value: Variant) -> String:
	if typeof(value) == TYPE_STRING and KNOWN_TYPES.has(value):
		return value
	return "hello"


func _is_simulation_type(message_type: String) -> bool:
	return SIMULATION_TYPES.has(message_type)


func _valid_session_id(value: String) -> bool:
	if value.is_empty() or value.length() > 64:
		return false
	for index: int in range(value.length()):
		var character := value.unicode_at(index)
		var is_lowercase := character >= 97 and character <= 122
		var is_digit := character >= 48 and character <= 57
		if not is_lowercase and not is_digit and (index == 0 or character != 45):
			return false
	return true


func _quit_with_code(code: int) -> void:
	_serving = false
	_quitting = true
	get_tree().quit(code)


func _respond_render(body: Dictionary) -> void:
	if not _candidate_loaded:
		_send_error(body, "bridge.no_candidate", "No candidate is loaded.")
		return
	var payload: Dictionary = body["payload"]
	var view := str(payload.get("view", ""))
	if view != "isometric" and view != "topdown":
		_send_error(
			body,
			"bridge.invalid_render_view",
			"view must be isometric or topdown",
		)
		return
	if _run_root.is_empty():
		_send_error(
			body,
			"bridge.render_unavailable",
			"no run root configured",
		)
		return
	if view == "isometric":
		_camera_rig.frame_isometric(_agent.global_position)
	else:
		_camera_rig.frame_topdown(_agent.global_position)
	var artifacts_dir := _run_root.path_join("artifacts")
	DirAccess.make_dir_recursive_absolute(artifacts_dir)
	var filename := "render-%s-%d.png" % [view, int(body["tick_id"])]
	var result: Dictionary = await _camera_rig.capture(
		artifacts_dir.path_join(filename)
	)
	if not result.get("ok", false):
		_send_error(
			body,
			"bridge.render_unavailable",
			str(result.get("error", "capture failed")),
		)
		return
	_send_success(
		body,
		{
			"path": "artifacts/" + filename,
			"byte_count": int(result["byte_count"]),
			"sha256": str(result["sha256"]),
		}
	)


func _respond_probe(body: Dictionary) -> void:
	if not _candidate_loaded:
		_send_error(body, "bridge.no_candidate", "No candidate is loaded.")
		return
	if _nav == null or _nav.status() != "ready":
		_send_error(
			body,
			"bridge.navigation_not_ready",
			"Navigation is not ready.",
		)
		return
	var payload: Dictionary = body["payload"]
	var landmark_value: Variant = payload.get("target_landmark_id")
	var radius_value: Variant = payload.get("success_radius_m")
	var max_ticks_value: Variant = _integer_at_least(payload.get("max_ticks"), 1)
	var stuck_value: Variant = _integer_at_least(
		payload.get("stuck_timeout_ticks"), 1
	)
	var fingerprint_value: Variant = payload.get("probe_fingerprint")
	if (
		typeof(landmark_value) != TYPE_STRING
		or (
			typeof(radius_value) != TYPE_FLOAT
			and typeof(radius_value) != TYPE_INT
		)
		or max_ticks_value == null
		or stuck_value == null
		or typeof(fingerprint_value) != TYPE_STRING
	):
		_send_error(body, "bridge.invalid_probe", "invalid probe payload")
		return
	var target_value: Variant = _find_node_origin(
		_candidate, str(landmark_value)
	)
	if target_value == null:
		_send_error(
			body,
			"bridge.unknown_landmark",
			"target landmark not in scene",
		)
		return
	var target: Vector3 = target_value
	var episode: Dictionary = await _agent.run_episode(
		target,
		float(radius_value),
		int(max_ticks_value),
		int(stuck_value),
		AGENT_SPEED,
	)
	_send_success(
		body,
		{
			"probe_fingerprint": str(fingerprint_value),
			"terminal_reason": str(episode["terminal_reason"]),
			"ticks_used": int(episode["ticks_used"]),
			"final_geodesic_distance_m": float(
				episode["final_geodesic_distance_m"]
			),
			"path_length_m": float(episode["path_length_m"]),
			"collisions": int(episode["collisions"]),
			"stuck_recoveries": 0,
			"planned_path_length_m": float(episode["planned_path_length_m"]),
		}
	)
