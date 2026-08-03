extends SceneTree

const Protocol = preload("res://bridge/protocol.gd")
const ArtifactLoader = preload("res://scene/artifact_loader.gd")
const Materializer = preload("res://scene/materializer.gd")
const NavRuntime = preload("res://scene/nav_runtime.gd")
const AgentController = preload("res://scene/agent_controller.gd")
const CameraRig = preload("res://scene/camera_rig.gd")
const TMP_ROOT_RES := "res://.godot/tmp_tests"

var _checks := 0
var _failures := 0
var _tmp_root := ""


func _initialize() -> void:
	_run_everything()


func _run_everything() -> void:
	_tmp_root = ProjectSettings.globalize_path(TMP_ROOT_RES)
	_cleanup_temp_dir()
	await _run_all()
	_cleanup_temp_dir()
	print("RESULT: %d checks, %d failures" % [_checks, _failures])
	quit(0 if _failures == 0 else 1)


func check(condition: bool, name: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		print("FAIL %s" % name)


func _run_all() -> void:
	_test_protocol_codec()
	_test_in_flight_accounting()
	_test_decoder_length_surface()
	_test_artifact_loader()
	_test_materializer()
	_test_visual_extensions()
	_test_organic_visuals()
	_test_sky_and_backdrop()
	_test_organic_kits()
	_test_spine_json()
	await _test_navigation_runtime()
	await _test_camera_rig()


func _test_protocol_codec() -> void:
	_test_protocol_roundtrip()
	_test_protocol_split_reassembly()
	_test_protocol_header_rejections()
	_test_protocol_malformed_bodies()
	_test_protocol_poisoning()
	_test_protocol_oversize_encode()


func _test_protocol_roundtrip() -> void:
	var first := _representative_request(1)
	var normalized_first := _json_normalize(first)
	var first_frame: PackedByteArray = Protocol.encode_frame(first)
	var single_decoder := Protocol.FrameDecoder.new()
	var single_result: Dictionary = single_decoder.feed(first_frame)
	var single_bodies := _result_bodies(single_result)
	var single_body: Dictionary = {}
	if single_bodies.size() == 1 and typeof(single_bodies[0]) == TYPE_DICTIONARY:
		single_body = single_bodies[0]

	check(
		single_result.get("ok", false)
		and single_bodies.size() == 1
		and single_body == normalized_first,
		"protocol roundtrip yields one normalized body",
	)
	check(
		single_body.get("request_id") == 1
		and single_body.get("payload", {}).get("integer_value") == 7,
		"protocol roundtrip preserves integer values",
	)
	check(
		single_body.has("tick_id") and single_body["tick_id"] == null,
		"protocol roundtrip preserves null",
	)

	var second := _representative_request(2)
	var normalized_second := _json_normalize(second)
	var concatenated := first_frame.duplicate()
	concatenated.append_array(Protocol.encode_frame(second))
	var multi_result: Dictionary = Protocol.FrameDecoder.new().feed(concatenated)
	var multi_bodies := _result_bodies(multi_result)
	check(
		multi_result.get("ok", false)
		and multi_bodies.size() == 2
		and multi_bodies[0] == normalized_first
		and multi_bodies[1] == normalized_second,
		"protocol concatenated frames preserve order",
	)


func _test_protocol_split_reassembly() -> void:
	var expected := _json_normalize(_representative_request(1))
	var frame: PackedByteArray = Protocol.encode_frame(_representative_request(1))
	var decoder := Protocol.FrameDecoder.new()
	var intermediate_ok := true
	var final_result: Dictionary = {}
	var offset := 0
	while offset < frame.size():
		var chunk_end: int = mini(offset + 3, frame.size())
		var result: Dictionary = decoder.feed(frame.slice(offset, chunk_end))
		if chunk_end < frame.size():
			var bodies := _result_bodies(result)
			if not result.get("ok", false) or not bodies.is_empty():
				intermediate_ok = false
		else:
			final_result = result
		offset = chunk_end

	check(
		intermediate_ok,
		"protocol split intermediate chunks remain incomplete",
	)
	var final_bodies := _result_bodies(final_result)
	check(
		final_result.get("ok", false)
		and final_bodies.size() == 1
		and final_bodies[0] == expected,
		"protocol split final chunk yields body",
	)


func _test_protocol_header_rejections() -> void:
	var empty_result: Dictionary = Protocol.FrameDecoder.new().feed(_header_for_length(0))
	check(
		_error_is(empty_result, "empty frame"),
		"protocol rejects empty frame header",
	)

	var oversize_result: Dictionary = Protocol.FrameDecoder.new().feed(
		_header_for_length(Protocol.MAX_CONTROL_MESSAGE_BYTES + 1)
	)
	check(
		_error_is(oversize_result, "frame exceeds control message limit"),
		"protocol rejects oversize frame header",
	)


func _test_protocol_malformed_bodies() -> void:
	var malformed_result: Dictionary = Protocol.FrameDecoder.new().feed(
		_frame_for_text("{not json")
	)
	check(
		_error_is(malformed_result, "malformed json frame"),
		"protocol rejects malformed json body",
	)

	var array_result: Dictionary = Protocol.FrameDecoder.new().feed(
		_frame_for_text("[1,2]")
	)
	check(
		_error_is(array_result, "frame must be a json object"),
		"protocol rejects non-object json body",
	)


func _test_protocol_poisoning() -> void:
	var decoder := Protocol.FrameDecoder.new()
	decoder.feed(_header_for_length(0))
	var poisoned_result: Dictionary = decoder.feed(
		Protocol.encode_frame(_representative_request(1))
	)
	check(
		_error_is(poisoned_result, "framing decoder already failed"),
		"protocol decoder stays poisoned after error",
	)


func _test_protocol_oversize_encode() -> void:
	var oversized_frame: PackedByteArray = Protocol.encode_frame(
		{"value": "x".repeat(Protocol.MAX_CONTROL_MESSAGE_BYTES)}
	)
	check(
		oversized_frame.is_empty(),
		"protocol oversize encode returns empty bytes",
	)


func _test_in_flight_accounting() -> void:
	check(
		not Protocol.in_flight_exceeded(0, Protocol.MAX_IN_FLIGHT_BYTES),
		"in-flight exactly at cap passes",
	)
	check(
		Protocol.in_flight_exceeded(0, Protocol.MAX_IN_FLIGHT_BYTES + 1),
		"in-flight one over cap fails",
	)
	check(
		Protocol.in_flight_exceeded(1, Protocol.MAX_IN_FLIGHT_BYTES),
		"in-flight buffered bytes count",
	)


func _test_decoder_length_surface() -> void:
	var frame: PackedByteArray = Protocol.encode_frame(_representative_request(1))
	var body_length := frame.size() - 4
	var decoder := Protocol.FrameDecoder.new()
	var result: Dictionary = decoder.feed(frame)
	check(
		result.get("ok", false)
		and result.get("body_lengths", []) == [body_length],
		"decoder reports body length",
	)
	check(
		decoder.buffered_bytes() == 0,
		"decoder buffered empty after full frame",
	)
	var partial := Protocol.FrameDecoder.new()
	var partial_result: Dictionary = partial.feed(frame.slice(0, 10))
	check(
		partial_result.get("ok", false)
		and _result_bodies(partial_result).is_empty()
		and partial_result.get("body_lengths", []) == []
		and partial.buffered_bytes() == 10,
		"decoder buffers partial frame with empty lengths",
	)


func _test_artifact_loader() -> void:
	_prepare_temp_dir()
	_test_artifact_relpath_acceptance()
	_test_artifact_relpath_rejections()

	var x_path := _tmp_root.path_join("artifacts/x.bin")
	_write_bytes(x_path, "hello".to_utf8_buffer())
	var ref_x := _artifact_ref(
		"artifacts/x.bin",
		5,
		FileAccess.get_sha256(x_path),
	)
	_test_artifact_verify_ref_pass(ref_x)
	_test_artifact_size_mismatch(ref_x)
	_test_artifact_digest_mismatch(ref_x)
	_test_artifact_missing_file()
	_test_artifact_manifest(ref_x)
	_test_artifact_symlink_components()


func _test_artifact_relpath_acceptance() -> void:
	check(
		ArtifactLoader.validate_relpath("artifacts/a.glb") == "",
		"artifact relpath accepts normal path",
	)


func _test_artifact_relpath_rejections() -> void:
	var rejected_paths := {
		"empty": "",
		"overlong": "a".repeat(513),
		"backslash": "a\\b.bin",
		"drive": "c:/x.bin",
		"absolute": "/abs.bin",
		"empty segment": "a//b.bin",
		"dot segment": "./a.bin",
		"parent segment": "a/../b.bin",
	}
	for case_name: String in rejected_paths:
		check(
			not ArtifactLoader.validate_relpath(rejected_paths[case_name]).is_empty(),
			"artifact relpath rejects %s" % case_name,
		)


func _test_artifact_verify_ref_pass(ref_x: Dictionary) -> void:
	check(
		ArtifactLoader.verify_ref(_tmp_root, ref_x) == "",
		"artifact verify_ref accepts matching file",
	)


func _test_artifact_size_mismatch(ref_x: Dictionary) -> void:
	var wrong_size := ref_x.duplicate(true)
	wrong_size["byte_count"] = 6
	check(
		ArtifactLoader.verify_ref(_tmp_root, wrong_size) == "size mismatch",
		"artifact verify_ref detects size mismatch",
	)


func _test_artifact_digest_mismatch(ref_x: Dictionary) -> void:
	var wrong_digest := ref_x.duplicate(true)
	var digest: String = wrong_digest["sha256"]
	var replacement := "1" if digest.begins_with("0") else "0"
	wrong_digest["sha256"] = replacement + digest.substr(1)
	check(
		ArtifactLoader.verify_ref(_tmp_root, wrong_digest) == "digest mismatch",
		"artifact verify_ref detects digest mismatch",
	)


func _test_artifact_missing_file() -> void:
	var missing_ref := _artifact_ref(
		"artifacts/never.bin",
		0,
		"0".repeat(64),
	)
	check(
		ArtifactLoader.verify_ref(_tmp_root, missing_ref) == "missing artifact file",
		"artifact verify_ref detects missing file",
	)


func _test_artifact_manifest(ref_x: Dictionary) -> void:
	var y_path := _tmp_root.path_join("artifacts/y.bin")
	_write_bytes(y_path, "world".to_utf8_buffer())
	var ref_y := _artifact_ref(
		"artifacts/y.bin",
		5,
		FileAccess.get_sha256(y_path),
	)
	var manifest := {
		"root": "artifacts",
		"entries": [ref_x, ref_y],
	}
	check(
		ArtifactLoader.verify_manifest(_tmp_root, manifest) == "",
		"artifact manifest accepts present files",
	)

	DirAccess.remove_absolute(y_path)
	check(
		ArtifactLoader.verify_manifest(_tmp_root, manifest)
		== "artifacts/y.bin: missing artifact file",
		"artifact manifest reports missing entry path",
	)


func _test_artifact_symlink_components() -> void:
	var outside_dir := _tmp_root.path_join("outside")
	var root_b := _tmp_root.path_join("rootb")
	DirAccess.make_dir_recursive_absolute(outside_dir)
	DirAccess.make_dir_recursive_absolute(root_b.path_join("artifacts/real"))
	var outside_file := outside_dir.path_join("x.bin")
	_write_bytes(outside_file, "escape".to_utf8_buffer())
	var inside_file := root_b.path_join("artifacts/real/y.bin")
	_write_bytes(inside_file, "inside".to_utf8_buffer())

	var escape_link := root_b.path_join("artifacts/link")
	var inside_link := root_b.path_join("artifacts/alias")
	var escape_exit := OS.execute("/bin/ln", ["-s", outside_dir, escape_link])
	var inside_exit := OS.execute(
		"/bin/ln", ["-s", root_b.path_join("artifacts/real"), inside_link]
	)
	var artifacts_dir := DirAccess.open(root_b.path_join("artifacts"))
	check(
		escape_exit == 0
		and inside_exit == 0
		and artifacts_dir != null
		and artifacts_dir.is_link("link")
		and artifacts_dir.is_link("alias"),
		"artifact symlink fixtures created",
	)

	var escape_ref := _artifact_ref(
		"artifacts/link/x.bin",
		6,
		FileAccess.get_sha256(outside_file),
	)
	check(
		ArtifactLoader.verify_ref(root_b, escape_ref) == "symlink artifact rejected",
		"artifact intermediate symlink escape rejected",
	)

	var inside_ref := _artifact_ref(
		"artifacts/alias/y.bin",
		6,
		FileAccess.get_sha256(inside_file),
	)
	check(
		ArtifactLoader.verify_ref(root_b, inside_ref) == "symlink artifact rejected",
		"artifact inside-pointing symlink rejected",
	)


func _representative_request(request_id: int) -> Dictionary:
	return {
		"protocol_version": 1,
		"session_id": "session-1",
		"request_id": request_id,
		"type": "hello",
		"tick_id": null,
		"payload": {"integer_value": 7, "label": "codec"},
	}


func _json_normalize(message: Dictionary) -> Dictionary:
	var normalized: Variant = JSON.parse_string(JSON.stringify(message))
	if typeof(normalized) == TYPE_DICTIONARY:
		return normalized
	return {}


func _result_bodies(result: Dictionary) -> Array:
	var bodies: Variant = result.get("bodies", [])
	if typeof(bodies) == TYPE_ARRAY:
		return bodies
	return []


func _error_is(result: Dictionary, expected: String) -> bool:
	return not result.get("ok", true) and result.get("error", "") == expected


func _header_for_length(body_length: int) -> PackedByteArray:
	var header := PackedByteArray()
	header.resize(4)
	header[0] = (body_length >> 24) & 0xff
	header[1] = (body_length >> 16) & 0xff
	header[2] = (body_length >> 8) & 0xff
	header[3] = body_length & 0xff
	return header


func _frame_for_text(body_text: String) -> PackedByteArray:
	var body := body_text.to_utf8_buffer()
	var frame := _header_for_length(body.size())
	frame.append_array(body)
	return frame


func _artifact_ref(relative_path: String, byte_count: int, sha256: String) -> Dictionary:
	return {
		"path": relative_path,
		"media_type": "application/octet-stream",
		"byte_count": byte_count,
		"blake2b256": "0".repeat(64),
		"sha256": sha256,
		"producer": "godot-test-harness",
		"toolchain_version": "test",
	}


func _prepare_temp_dir() -> void:
	_cleanup_temp_dir()
	DirAccess.make_dir_recursive_absolute(_tmp_root.path_join("artifacts"))


func _write_bytes(path: String, bytes: PackedByteArray) -> void:
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return
	file.store_buffer(bytes)
	file.close()


func _cleanup_temp_dir() -> void:
	if not _tmp_root.is_empty():
		_remove_tree(_tmp_root)


func _remove_tree(path: String) -> void:
	if not DirAccess.dir_exists_absolute(path):
		return
	var directory := DirAccess.open(path)
	if directory == null:
		return
	directory.list_dir_begin()
	var entry := directory.get_next()
	while not entry.is_empty():
		var child := path.path_join(entry)
		if directory.current_is_dir():
			_remove_tree(child)
		else:
			DirAccess.remove_absolute(child)
		entry = directory.get_next()
	directory.list_dir_end()
	DirAccess.remove_absolute(path)


func _test_materializer() -> void:
	var result: Dictionary = Materializer.materialize(_materializer_candidate(), get_root())
	var root: Node3D = null
	if result.get("ok", false):
		root = result["root"]
	check(
		result.get("ok", false)
		and root != null
		and root.get_child_count() == 16
		and root.get_node_or_null("sun") is DirectionalLight3D
		and root.get_node_or_null("environment") is WorldEnvironment
		and root.get_node_or_null("apron") is MeshInstance3D,
		"materializer builds root with lighting",
	)
	if root == null:
		return

	var ground := root.get_node_or_null("ground")
	var crate := root.get_node_or_null("crate")
	var marker := root.get_node_or_null("marker")
	check(
		ground is StaticBody3D
		and crate is StaticBody3D
		and marker is Node3D
		and not (marker is StaticBody3D),
		"materializer bodies match colliders",
	)
	check(
		result["counts"] == {"bodies": 2, "visuals": 3, "navmesh": 2},
		"materializer counts are exact",
	)

	var ground_shape: Variant = null
	if ground != null and ground.get_node_or_null("collision") != null:
		ground_shape = ground.get_node("collision").shape
	check(
		ground_shape is BoxShape3D
		and (ground_shape as BoxShape3D).size == Vector3(40.0, 0.5, 40.0),
		"materializer box collider sized",
	)

	var ground_mesh: Variant = null
	var crate_mesh: Variant = null
	var marker_mesh: Variant = null
	if ground != null and ground.get_node_or_null("visual") != null:
		ground_mesh = ground.get_node("visual").mesh
	if crate != null and crate.get_node_or_null("visual") != null:
		crate_mesh = crate.get_node("visual").mesh
	if marker != null and marker.get_node_or_null("visual") != null:
		marker_mesh = marker.get_node("visual").mesh
	check(
		ground_mesh is PlaneMesh
		and (ground_mesh as PlaneMesh).size == Vector2(40.0, 40.0)
		and crate_mesh is BoxMesh
		and (crate_mesh as BoxMesh).size == Vector3(1.0, 1.0, 1.0)
		and marker_mesh is CylinderMesh
		and (marker_mesh as CylinderMesh).height == 1.5,
		"materializer meshes match visuals",
	)

	var y_scale: float = Materializer.visual_y_scale()
	var crate_visual: MeshInstance3D = crate.get_node("visual")
	var crate_collider: BoxShape3D = crate.get_node("collision").shape
	check(
		absf(crate_visual.scale.y - y_scale) < 0.001
		and absf(
			crate_visual.position.y - (-(1.0 - y_scale) * 0.5)
		) < 0.001
		and crate_collider.size == Vector3(1.0, 1.0, 1.0)
		and (ground.get_node("visual") as MeshInstance3D).scale.y == 1.0,
		"visual y-compression leaves colliders and ground untouched",
	)

	var apron_node: MeshInstance3D = root.get_node("apron")
	check(
		(apron_node.mesh as PlaneMesh).size == Vector2(240.0, 240.0)
		and apron_node.position.y < 0.0
		and apron_node.get_node_or_null("collision") == null
		and not apron_node.is_in_group(Materializer.NAVMESH_GROUP),
		"apron is visual-only surrounding terrain",
	)

	var ground_material: Variant = null
	var marker_material: Variant = null
	if ground_mesh != null:
		ground_material = ground.get_node("visual").material_override
	if marker_mesh != null:
		marker_material = marker.get_node("visual").material_override
	check(
		ground_material is StandardMaterial3D
		and (ground_material as StandardMaterial3D).albedo_color
		== Materializer.MATERIAL_COLORS["grass"]
		and marker_material is StandardMaterial3D
		and (marker_material as StandardMaterial3D).albedo_color
		== Materializer.MATERIAL_COLORS["default"],
		"materializer curated materials with fallback",
	)
	check(
		ground != null
		and str(ground.get_meta("semantic_id")) == "ground"
		and ground.is_in_group(Materializer.NAVMESH_GROUP)
		and crate.is_in_group(Materializer.NAVMESH_GROUP)
		and not marker.is_in_group(Materializer.NAVMESH_GROUP),
		"materializer metadata and navmesh group",
	)
	check(
		crate != null and crate.position == Vector3(2.0, 0.5, 3.0),
		"materializer applies transforms",
	)

	var bad_candidate := _materializer_candidate()
	var bad_nodes: Array = bad_candidate["scene"]["nodes"]
	var bad_node: Dictionary = (bad_nodes[2] as Dictionary).duplicate(true)
	bad_node["visual"] = {"shape": "pyramid", "radius": 1.0}
	bad_nodes[2] = bad_node
	var bad_result: Dictionary = Materializer.materialize(bad_candidate, get_root())
	check(
		not bad_result.get("ok", true)
		and str(bad_result.get("error", "")) == "unknown visual shape: pyramid",
		"materializer rejects unknown visual shape",
	)

	root.get_parent().remove_child(root)
	root.free()


func _test_visual_extensions() -> void:
	var candidate := {
		"scene": {
			"nodes": [
				_scene_node_dict(
					"terrain",
					Vector3(0.0, 0.0, 0.0),
					{
						"shape": "box",
						"dimensions": {"x": 20.0, "y": 0.5, "z": 20.0},
					},
					true,
					{
						"shape": "plane",
						"size_x": 20.0,
						"size_z": 20.0,
						"material": "palette.ground",
					},
				),
				_scene_node_dict(
					"crystal",
					Vector3(2.0, 0.9, 2.0),
					null,
					false,
					{
						"shape": "cylinder",
						"radius": 0.45,
						"top_radius": 0.0,
						"height": 1.8,
						"material": "violet_crystal",
					},
				),
			],
			"camera": {
				"follow_semantic_id": "terrain",
				"orthographic_size": 12.0,
				"fade_occluders": true,
			},
			"controller_semantic_id": "terrain",
			"materials": {
				"palette.ground": {"color": "#6a4ba3"},
				"violet_crystal": {
					"color": "#a36cff",
					"emission_color": "#7b3dff",
					"emission_strength": 1.2,
					"roughness": 0.25,
					"metallic": 0.15,
				},
			},
			"presentation": {
				"sky_top": "#180e32",
				"sky_horizon": "#593b82",
				"sun_color": "#c5a6ff",
				"sun_energy": 0.45,
				"ambient_color": "#443b62",
				"ambient_energy": 0.9,
			},
		},
		"manifest": {"root": "artifacts", "entries": []},
	}
	var result: Dictionary = Materializer.materialize(candidate, get_root())
	var root: Node3D = null
	if result.get("ok", false):
		root = result["root"]
	check(result.get("ok", false) and root != null, "visual extensions materialize")
	if root == null:
		return

	var terrain := root.get_node_or_null("terrain")
	var terrain_material: StandardMaterial3D = null
	if terrain != null and terrain.get_node_or_null("visual") != null:
		terrain_material = terrain.get_node("visual").material_override
	check(
		terrain_material != null
		and terrain_material.albedo_color.is_equal_approx(Color.html("#6a4ba3")),
		"material table drives albedo over curated fallback",
	)

	var crystal := root.get_node_or_null("crystal")
	var crystal_material: StandardMaterial3D = null
	if crystal != null and crystal.get_node_or_null("visual") != null:
		crystal_material = crystal.get_node("visual").material_override
	check(
		crystal_material != null
		and crystal_material.emission_enabled
		and crystal_material.emission.is_equal_approx(Color.html("#7b3dff"))
		and absf(crystal_material.emission_energy_multiplier - 1.2) < 0.001
		and absf(crystal_material.roughness - 0.25) < 0.001
		and absf(crystal_material.metallic - 0.15) < 0.001,
		"custom material emission, roughness, metallic applied",
	)

	var sun := root.get_node_or_null("sun") as DirectionalLight3D
	check(
		sun != null
		and absf(sun.light_energy - 0.45) < 0.001
		and sun.light_color.is_equal_approx(Color.html("#c5a6ff")),
		"presentation overrides sun color and energy",
	)

	var world_environment := root.get_node_or_null("environment") as WorldEnvironment
	var environment_ok := false
	if world_environment != null and world_environment.environment != null:
		var environment_resource := world_environment.environment
		var sky_material := environment_resource.sky.sky_material as ProceduralSkyMaterial
		environment_ok = (
			environment_resource.ambient_light_color.is_equal_approx(Color.html("#443b62"))
			and absf(environment_resource.ambient_light_energy - 0.9) < 0.001
			and sky_material.sky_top_color.is_equal_approx(Color.html("#180e32"))
			and sky_material.sky_horizon_color.is_equal_approx(Color.html("#593b82"))
		)
	check(environment_ok, "presentation overrides ambient and sky colors")

	root.get_parent().remove_child(root)
	root.free()


func _test_organic_visuals() -> void:
	var candidate := {
		"scene": {
			"nodes": [
				_scene_node_dict(
					"sphere_node",
					Vector3(0.0, 1.0, 0.0),
					null,
					false,
					{"shape": "sphere", "radius": 1.25, "material": "stone"},
				),
				_scene_node_dict(
					"cone_node",
					Vector3(2.0, 0.0, 0.0),
					null,
					false,
					{
						"shape": "cylinder",
						"radius": 0.5,
						"height": 2.0,
						"top_radius": 0.0,
						"material": "wood",
					},
				),
				_scene_node_dict(
					"ribbon_node",
					Vector3(0.0, 0.0, 0.0),
					null,
					false,
					{
						"shape": "ribbon",
						"points": [[0.0, 0.0], [1.0, 0.0], [2.0, 1.0]],
						"width": 0.4,
						"material": "dirt",
					},
				),
			],
			"camera": {
				"follow_semantic_id": "sphere_node",
				"orthographic_size": 14.0,
				"fade_occluders": true,
			},
			"controller_semantic_id": "sphere_node",
		},
		"manifest": {"root": "artifacts", "entries": []},
	}
	var result: Dictionary = Materializer.materialize(candidate, get_root())
	var root: Node3D = null
	if result.get("ok", false):
		root = result["root"]
	check(result.get("ok", false) and root != null, "organic visuals materialize")
	if root == null:
		return

	var sphere_node: Node3D = root.get_node_or_null("sphere_node")
	var cone_node: Node3D = root.get_node_or_null("cone_node")
	var ribbon_node: Node3D = root.get_node_or_null("ribbon_node")
	var sphere_mesh: Variant = null
	var cone_mesh: Variant = null
	var ribbon_visual: MeshInstance3D = null
	if sphere_node != null and sphere_node.get_node_or_null("visual") != null:
		sphere_mesh = sphere_node.get_node("visual").mesh
	if cone_node != null and cone_node.get_node_or_null("visual") != null:
		cone_mesh = cone_node.get_node("visual").mesh
	if ribbon_node != null and ribbon_node.get_node_or_null("visual") != null:
		ribbon_visual = ribbon_node.get_node("visual")

	check(
		sphere_mesh is SphereMesh
		and absf((sphere_mesh as SphereMesh).radius - 1.25) < 0.0001,
		"organic sphere mesh radius exact",
	)
	check(
		cone_mesh is CylinderMesh
		and absf((cone_mesh as CylinderMesh).top_radius - 0.0) < 0.0001
		and absf((cone_mesh as CylinderMesh).bottom_radius - 0.5) < 0.0001,
		"organic cone mesh top_radius zero",
	)
	check(
		ribbon_visual != null
		and ribbon_visual.mesh is ArrayMesh
		and (ribbon_visual.mesh as ArrayMesh).surface_get_array_len(0) > 24,
		"organic ribbon array mesh has enough vertices",
	)
	check(
		ribbon_node != null
		and not (ribbon_node is StaticBody3D)
		and ribbon_node.get_node_or_null("collision") == null,
		"organic ribbon has no collider",
	)
	check(
		ribbon_visual != null and absf(ribbon_visual.scale.y - 1.0) < 0.0001,
		"organic ribbon skips y-compression",
	)

	var ribbon_front_up := false
	if ribbon_visual != null and ribbon_visual.mesh is ArrayMesh:
		var arrays: Array = (
			ribbon_visual.mesh as ArrayMesh
		).surface_get_arrays(0)
		var verts: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
		var idx: PackedInt32Array = arrays[Mesh.ARRAY_INDEX]
		if idx.size() >= 3:
			var a: Vector3 = verts[idx[0]]
			var b: Vector3 = verts[idx[1]]
			var c: Vector3 = verts[idx[2]]
			# Godot front faces wind clockwise: from +Y the geometric
			# normal of a front-facing upward triangle points DOWN.
			ribbon_front_up = (b - a).cross(c - a).y < 0.0
	check(
		ribbon_front_up,
		"organic ribbon winds front-face up (visible from above)",
	)

	root.get_parent().remove_child(root)
	root.free()


func _test_sky_and_backdrop() -> void:
	var grass_result: Dictionary = Materializer.materialize(
		_materializer_candidate(),
		get_root(),
	)
	var grass_root: Node3D = null
	if grass_result.get("ok", false):
		grass_root = grass_result["root"]
	check(grass_result.get("ok", false) and grass_root != null, "sky grass candidate ok")
	if grass_root == null:
		return

	var world_env: WorldEnvironment = grass_root.get_node_or_null("environment")
	var env_res: Environment = null
	if world_env != null:
		env_res = world_env.environment
	var sky_mat: ProceduralSkyMaterial = null
	if (
		env_res != null
		and env_res.sky != null
		and env_res.sky.sky_material is ProceduralSkyMaterial
	):
		sky_mat = env_res.sky.sky_material
	check(
		env_res != null
		and env_res.background_mode == Environment.BG_SKY
		and sky_mat != null,
		"environment uses BG_SKY with ProceduralSkyMaterial",
	)

	var mound_count := 0
	var mounds_ok := true
	for child: Node in grass_root.get_children():
		if not str(child.name).begins_with("mound_"):
			continue
		mound_count += 1
		if not (child is MeshInstance3D):
			mounds_ok = false
			continue
		var mound: MeshInstance3D = child
		if mound.get_node_or_null("collision") != null:
			mounds_ok = false
		if mound.is_in_group(Materializer.NAVMESH_GROUP):
			mounds_ok = false
	check(
		mound_count == 10 and mounds_ok,
		"exactly 10 visual-only mound_* backdrop spheres",
	)

	var grass_top: Color = Color.BLACK
	if sky_mat != null:
		grass_top = sky_mat.sky_top_color

	grass_root.get_parent().remove_child(grass_root)
	grass_root.free()

	var snow_candidate := _materializer_candidate()
	var snow_nodes: Array = snow_candidate["scene"]["nodes"]
	var ground_node: Dictionary = (snow_nodes[0] as Dictionary).duplicate(true)
	var ground_visual: Dictionary = (ground_node["visual"] as Dictionary).duplicate(true)
	ground_visual["material"] = "snow"
	ground_node["visual"] = ground_visual
	snow_nodes[0] = ground_node
	var snow_result: Dictionary = Materializer.materialize(snow_candidate, get_root())
	var snow_root: Node3D = null
	if snow_result.get("ok", false):
		snow_root = snow_result["root"]
	check(snow_result.get("ok", false) and snow_root != null, "sky snow candidate ok")
	if snow_root == null:
		return
	var snow_env: WorldEnvironment = snow_root.get_node_or_null("environment")
	var snow_top: Color = Color.BLACK
	if (
		snow_env != null
		and snow_env.environment != null
		and snow_env.environment.sky != null
		and snow_env.environment.sky.sky_material is ProceduralSkyMaterial
	):
		snow_top = (
			snow_env.environment.sky.sky_material as ProceduralSkyMaterial
		).sky_top_color
	check(
		grass_top != snow_top,
		"grass and snow ground palettes differ in sky top color",
	)
	snow_root.get_parent().remove_child(snow_root)
	snow_root.free()


func _test_organic_kits() -> void:
	# Compiled pine kit shape: cylinder trunk + cone canopy (top_radius=0).
	var pine_candidate := {
		"scene": {
			"nodes": [
				_scene_node_dict(
					"pine_trunk",
					Vector3(0.0, 0.7, 0.0),
					{
						"shape": "cylinder",
						"dimensions": {"radius": 0.18, "height": 1.4},
					},
					true,
					{
						"shape": "cylinder",
						"radius": 0.18,
						"height": 1.4,
						"material": "wood",
					},
				),
				_scene_node_dict(
					"pine_canopy",
					Vector3(0.0, 2.0, 0.0),
					{
						"shape": "cylinder",
						"dimensions": {"radius": 0.9, "height": 1.8},
					},
					true,
					{
						"shape": "cylinder",
						"radius": 0.9,
						"height": 1.8,
						"top_radius": 0.0,
						"material": "grass",
					},
				),
			],
			"camera": {
				"follow_semantic_id": "pine_trunk",
				"orthographic_size": 14.0,
				"fade_occluders": true,
			},
			"controller_semantic_id": "pine_trunk",
		},
		"manifest": {"root": "artifacts", "entries": []},
	}
	var pine_result: Dictionary = Materializer.materialize(pine_candidate, get_root())
	var pine_root: Node3D = null
	if pine_result.get("ok", false):
		pine_root = pine_result["root"]
	check(pine_result.get("ok", false) and pine_root != null, "pine kit materializes")
	if pine_root != null:
		var trunk: Node3D = pine_root.get_node_or_null("pine_trunk")
		var canopy: Node3D = pine_root.get_node_or_null("pine_canopy")
		var trunk_mesh: Variant = null
		var canopy_mesh: Variant = null
		if trunk != null and trunk.get_node_or_null("visual") != null:
			trunk_mesh = trunk.get_node("visual").mesh
		if canopy != null and canopy.get_node_or_null("visual") != null:
			canopy_mesh = canopy.get_node("visual").mesh
		check(
			trunk_mesh is CylinderMesh
			and canopy_mesh is CylinderMesh
			and absf((canopy_mesh as CylinderMesh).top_radius - 0.0) < 0.0001,
			"pine kit is trunk cylinder plus cone canopy",
		)
		pine_root.get_parent().remove_child(pine_root)
		pine_root.free()

	var boulder_candidate := {
		"scene": {
			"nodes": [
				_scene_node_dict(
					"boulder_large",
					Vector3(0.0, 0.35, 0.0),
					{
						"shape": "cylinder",
						"dimensions": {"radius": 0.9, "height": 1.8},
					},
					true,
					{"shape": "sphere", "radius": 0.9, "material": "rock"},
				),
				_scene_node_dict(
					"boulder_small",
					Vector3(0.55, 0.25, 0.2),
					{
						"shape": "cylinder",
						"dimensions": {"radius": 0.5, "height": 1.0},
					},
					true,
					{"shape": "sphere", "radius": 0.5, "material": "rock"},
				),
			],
			"camera": {
				"follow_semantic_id": "boulder_large",
				"orthographic_size": 14.0,
				"fade_occluders": true,
			},
			"controller_semantic_id": "boulder_large",
		},
		"manifest": {"root": "artifacts", "entries": []},
	}
	var boulder_result: Dictionary = Materializer.materialize(
		boulder_candidate,
		get_root(),
	)
	var boulder_root: Node3D = null
	if boulder_result.get("ok", false):
		boulder_root = boulder_result["root"]
	check(
		boulder_result.get("ok", false) and boulder_root != null,
		"boulder kit materializes",
	)
	if boulder_root == null:
		return
	var large: Node3D = boulder_root.get_node_or_null("boulder_large")
	var small: Node3D = boulder_root.get_node_or_null("boulder_small")
	var large_mesh: Variant = null
	var small_mesh: Variant = null
	if large != null and large.get_node_or_null("visual") != null:
		large_mesh = large.get_node("visual").mesh
	if small != null and small.get_node_or_null("visual") != null:
		small_mesh = small.get_node("visual").mesh
	check(
		large_mesh is SphereMesh
		and small_mesh is SphereMesh
		and absf((large_mesh as SphereMesh).radius - 0.9) < 0.0001
		and absf((small_mesh as SphereMesh).radius - 0.5) < 0.0001,
		"boulder kit builds two rock spheres",
	)
	boulder_root.get_parent().remove_child(boulder_root)
	boulder_root.free()


func _materializer_candidate() -> Dictionary:
	return {
		"scene": {
			"nodes": [
				_scene_node_dict(
					"ground",
					Vector3(0.0, 0.0, 0.0),
					{
						"shape": "box",
						"dimensions": {"x": 40.0, "y": 0.5, "z": 40.0},
					},
					true,
					{
						"shape": "plane",
						"size_x": 40.0,
						"size_z": 40.0,
						"material": "grass",
					},
				),
				_scene_node_dict(
					"crate",
					Vector3(2.0, 0.5, 3.0),
					{
						"shape": "box",
						"dimensions": {"x": 1.0, "y": 1.0, "z": 1.0},
					},
					true,
					{
						"shape": "box",
						"size": [1.0, 1.0, 1.0],
						"material": "wood",
					},
				),
				_scene_node_dict(
					"marker",
					Vector3(-3.0, 0.0, 1.0),
					null,
					false,
					{
						"shape": "cylinder",
						"radius": 0.3,
						"height": 1.5,
						"material": "copper",
					},
				),
			],
			"camera": {
				"follow_semantic_id": "ground",
				"orthographic_size": 14.0,
				"fade_occluders": true,
			},
			"controller_semantic_id": "ground",
		},
		"manifest": {"root": "artifacts", "entries": []},
	}


func _scene_node_dict(
	node_id: String,
	origin: Vector3,
	collider: Variant,
	navmesh_contributor: bool,
	visual: Variant,
) -> Dictionary:
	var node := {
		"node_id": node_id,
		"semantic_id": node_id,
		"transform": {
			"origin": {"x": origin.x, "y": origin.y, "z": origin.z},
			"basis_x": {"x": 1.0, "y": 0.0, "z": 0.0},
			"basis_y": {"x": 0.0, "y": 1.0, "z": 0.0},
			"basis_z": {"x": 0.0, "y": 0.0, "z": 1.0},
		},
		"mesh": null,
		"collider": collider,
		"navmesh_contributor": navmesh_contributor,
		"fade_group": "",
	}
	if visual != null:
		node["visual"] = visual
	return node


func _test_navigation_runtime() -> void:
	await physics_frame
	var stage := Node3D.new()
	stage.name = "nav_stage"
	get_root().add_child(stage)
	var materialized: Dictionary = Materializer.materialize(
		_materializer_candidate(), stage
	)
	var nav: Node3D = NavRuntime.new()
	nav.name = "nav_runtime"
	stage.add_child(nav)
	var statuses: Array[String] = []
	nav.status_changed.connect(
		func(status: String) -> void: statuses.append(status)
	)

	check(
		materialized.get("ok", false) and nav.status() == "unloaded",
		"nav starts unloaded",
	)

	var from := Vector3(-15.0, 0.5, -15.0)
	var to := Vector3(15.0, 0.5, 15.0)
	nav.setup(0.5)
	await nav.bake(15.0, from, to)
	check(
		nav.status() == "ready"
		and statuses == ["parsing", "baking", "ready"],
		"nav bake reaches ready with real transitions",
	)
	check(
		nav.status() == "ready"
		and _region_polygon_count(nav) > 0,
		"nav bake produced polygons",
	)

	var path: PackedVector3Array = nav.path_between(from, to)
	var length: float = nav.path_length(path)
	check(
		path.size() >= 2 and length >= 40.0 and length < 80.0,
		"nav path query returns plausible route",
	)

	var agent_body: CharacterBody3D = AgentController.new()
	agent_body.name = "agent_body"
	stage.add_child(agent_body)
	agent_body.global_position = Vector3(-15.0, 1.5, -15.0)
	agent_body.setup(nav.map_rid())
	var episode: Dictionary = await agent_body.run_episode(
		Vector3(15.0, 0.5, 15.0), 1.0, 900, 120, 6.0
	)
	check(
		episode["terminal_reason"] == "arrived"
		and int(episode["ticks_used"]) < 900
		and float(episode["path_length_m"]) > 35.0
		and float(episode["path_length_m"]) < 60.0
		and float(episode["planned_path_length_m"]) >= 40.0
		and float(episode["final_geodesic_distance_m"]) <= 2.0,
		"agent traverses flat ground",
	)

	var wander_start: Vector3 = agent_body.global_position
	agent_body.start_wander(4.0)
	for i: int in range(40):
		await physics_frame
	agent_body.stop_wander()
	var wander_position: Vector3 = agent_body.global_position
	var closest: Vector3 = NavigationServer3D.map_get_closest_point(
		nav.map_rid(), wander_position
	)
	check(
		wander_position.distance_to(wander_start) > 0.5
		and wander_position.distance_to(closest) < 1.5,
		"wander moves the agent and stays on the navmesh",
	)

	# Stuck recovery: an unreachable target (straight off the west mesh edge)
	# must not pin the agent grinding at the boundary — navigation finishes at
	# the clamped path end and the watchdog picks a fresh target.
	agent_body.global_position = Vector3(-15.0, 1.5, 0.0)
	agent_body.start_wander(4.0)
	agent_body._set_wander_target(Vector3(-200.0, 0.0, 0.0))
	var retargets_before: int = agent_body.wander_retargets()
	for i: int in range(240):
		await physics_frame
	agent_body.stop_wander()
	check(
		agent_body.wander_retargets() > retargets_before,
		"wander recovers from an unreachable target",
	)

	get_root().remove_child(stage)
	stage.free()


func _region_polygon_count(nav: Node3D) -> int:
	var region: NavigationRegion3D = nav.get_node_or_null("region")
	if region == null or region.navigation_mesh == null:
		return 0
	return region.navigation_mesh.get_polygon_count()


func _test_camera_rig() -> void:
	var rig: Node3D = CameraRig.new()
	rig.name = "camera_rig"
	get_root().add_child(rig)
	rig.setup(14.0, Vector2i(320, 180))
	var camera: Camera3D = rig.get_node("viewport/camera")
	var target := Vector3(2.0, 0.5, 3.0)

	rig.frame_isometric(target)
	var expected_position: Vector3 = (
		target + camera.transform.basis.z * 30.0
	)
	check(
		camera.projection == Camera3D.PROJECTION_ORTHOGONAL
		and absf(camera.size - 14.0) < 0.001
		and camera.rotation_degrees.distance_to(
			Vector3(-CameraRig.iso_pitch_degrees(), 45.0, 0.0)
		) < 0.01
		and camera.position.distance_to(expected_position) < 0.001,
		"camera iso framing exact",
	)

	rig.frame_topdown(target)
	check(
		camera.rotation_degrees.distance_to(Vector3(-90.0, 0.0, 0.0)) < 0.01
		and camera.position.distance_to(Vector3(2.0, 30.0, 3.0)) < 0.001
		and camera.projection == Camera3D.PROJECTION_ORTHOGONAL,
		"camera topdown framing exact",
	)

	var mover := Node3D.new()
	get_root().add_child(mover)
	mover.global_position = Vector3(5.0, 0.0, 5.0)
	rig.set_follow_target(mover)
	rig.frame_isometric(mover.global_position)
	mover.global_position = Vector3(9.0, 0.0, -2.0)
	await process_frame
	await process_frame
	var follow_expected: Vector3 = (
		mover.global_position + camera.transform.basis.z * 30.0
	)
	check(
		camera.position.distance_to(follow_expected) < 0.01,
		"camera follows moving target each frame",
	)
	rig.frame_topdown(mover.global_position)
	var topdown_position: Vector3 = camera.position
	mover.global_position = Vector3(1.0, 0.0, 1.0)
	await process_frame
	check(
		camera.position.distance_to(topdown_position) < 0.001,
		"topdown framing suspends follow",
	)
	mover.queue_free()

	var result: Dictionary = await rig.capture(
		_tmp_root.path_join("camera_probe.png")
	)
	check(
		not result.get("ok", true)
		and str(result.get("error", "")) == "rendering unavailable",
		"camera capture degrades gracefully headless",
	)

	get_root().remove_child(rig)
	rig.free()


func _test_spine_json() -> void:
	var file := FileAccess.open(
		"res://../examples/spine/candidate-scene.json", FileAccess.READ
	)
	var parsed: Variant = null
	if file != null:
		parsed = JSON.parse_string(file.get_as_text())
	check(
		typeof(parsed) == TYPE_DICTIONARY,
		"spine json parses",
	)
	if typeof(parsed) != TYPE_DICTIONARY:
		return
	var stage := Node3D.new()
	stage.name = "spine_stage"
	get_root().add_child(stage)
	var result: Dictionary = Materializer.materialize(parsed, stage)
	var nodes: Array = (parsed as Dictionary)["scene"]["nodes"]
	check(
		result.get("ok", false) and nodes.size() == 10,
		"spine json materializes with exact node count",
	)
	get_root().remove_child(stage)
	stage.free()
