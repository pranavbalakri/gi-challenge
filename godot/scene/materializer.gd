extends RefCounted

const MATERIAL_COLORS: Dictionary = {
	"default": Color(0.604, 0.604, 0.604),
	"grass": Color(0.310, 0.541, 0.239),
	"dirt": Color(0.478, 0.357, 0.227),
	"stone": Color(0.690, 0.675, 0.643),
	"rock": Color(0.431, 0.416, 0.388),
	"wood": Color(0.541, 0.420, 0.275),
	"water": Color(0.247, 0.435, 0.682),
	"snow": Color(0.910, 0.925, 0.945),
}
const NAVMESH_GROUP := "navmesh_source"
const DEFAULT_VISUAL_Y_SCALE := 0.7


static func visual_y_scale() -> float:
	# Presentation-only vertical compression of MESHES about y=0: colliders,
	# navmesh carving, and every Python-side contract stay untouched.
	# ENVMAKER_VISUAL_Y_SCALE overrides (clamped 0.3..1.0).
	var raw := OS.get_environment("ENVMAKER_VISUAL_Y_SCALE")
	if raw.is_empty() or not raw.is_valid_float():
		return DEFAULT_VISUAL_Y_SCALE
	return clampf(raw.to_float(), 0.3, 1.0)


static func materialize(candidate: Dictionary, parent: Node) -> Dictionary:
	var scene_value: Variant = candidate.get("scene")
	if typeof(scene_value) != TYPE_DICTIONARY:
		return {"ok": false, "error": "invalid candidate"}
	var scene: Dictionary = scene_value
	var nodes_value: Variant = scene.get("nodes")
	if typeof(nodes_value) != TYPE_ARRAY:
		return {"ok": false, "error": "invalid candidate"}
	var nodes: Array = nodes_value

	# Optional visual-extension tables (absent for legacy candidates).
	var materials: Dictionary = {}
	var materials_value: Variant = scene.get("materials", null)
	if typeof(materials_value) == TYPE_DICTIONARY:
		materials = materials_value
	var presentation: Dictionary = {}
	var presentation_value: Variant = scene.get("presentation", null)
	if typeof(presentation_value) == TYPE_DICTIONARY:
		presentation = presentation_value

	var root := Node3D.new()
	root.name = "materialized"
	var body_count := 0
	var visual_count := 0
	var navmesh_count := 0
	for node_value: Variant in nodes:
		if typeof(node_value) != TYPE_DICTIONARY:
			root.free()
			return {"ok": false, "error": "invalid node: missing node_id"}
		var node: Dictionary = node_value
		var node_result: Dictionary = _build_node(node, materials)
		if not node_result.get("ok", false):
			root.free()
			return {"ok": false, "error": node_result.get("error", "")}
		var built_node: Node3D = node_result["node"]
		root.add_child(built_node)
		if built_node is StaticBody3D:
			body_count += 1
		if bool(node_result.get("has_visual", false)):
			visual_count += 1
		if bool(node.get("navmesh_contributor", false)):
			navmesh_count += 1

	var ground_color: Color = MATERIAL_COLORS["default"]
	var ground_material_name := "default"
	var ground_size := Vector2.ZERO
	var ground_origin := Vector3.ZERO
	for node_value: Variant in nodes:
		var node: Dictionary = node_value
		var visual_value: Variant = node.get("visual", null)
		if typeof(visual_value) != TYPE_DICTIONARY:
			continue
		var visual: Dictionary = visual_value
		if str(visual.get("shape", "")) != "plane":
			continue
		var size := Vector2(
			float(visual.get("size_x", 0.0)),
			float(visual.get("size_z", 0.0)),
		)
		if size.x * size.y <= ground_size.x * ground_size.y:
			continue
		ground_size = size
		ground_material_name = str(visual.get("material", "default"))
		ground_color = _resolve_color(ground_material_name, materials)
		var transform_result: Dictionary = _parse_transform(node.get("transform"))
		if transform_result.get("ok", false):
			var ground_transform: Transform3D = transform_result["transform"]
			ground_origin = ground_transform.origin

	var palette: Dictionary = _sky_palette(ground_material_name)
	# Environment-declared presentation overrides the derived defaults.
	if presentation.has("sky_top"):
		palette["sky_top"] = Color.html(str(presentation["sky_top"]))
	if presentation.has("sky_horizon"):
		palette["sky_horizon"] = Color.html(str(presentation["sky_horizon"]))
	if presentation.has("sun_color"):
		palette["sun"] = Color.html(str(presentation["sun_color"]))

	if ground_size != Vector2.ZERO:
		# Surrounding terrain margin: kills the floating-board look without
		# touching collision or navigation (visual only, slightly sunken).
		var apron := MeshInstance3D.new()
		apron.name = "apron"
		var apron_mesh := PlaneMesh.new()
		apron_mesh.size = ground_size * 6.0
		apron.mesh = apron_mesh
		var apron_material := StandardMaterial3D.new()
		apron_material.albedo_color = ground_color.darkened(0.22)
		apron.material_override = apron_material
		apron.position = Vector3(ground_origin.x, -0.06, ground_origin.z)
		root.add_child(apron)
		_add_backdrop_mounds(root, ground_origin, ground_size, ground_color)

	# Flat, readable lighting: strong ambient, gentle steep sun, soft shadows.
	var sun := DirectionalLight3D.new()
	sun.name = "sun"
	sun.rotation_degrees = Vector3(-65.0, -30.0, 0.0)
	sun.light_energy = 0.55
	if presentation.has("sun_energy") and _is_number(presentation["sun_energy"]):
		sun.light_energy = clampf(float(presentation["sun_energy"]), 0.0, 2.0)
	sun.light_color = palette["sun"]
	sun.shadow_enabled = true
	root.add_child(sun)

	var sky_material := ProceduralSkyMaterial.new()
	sky_material.sky_top_color = palette["sky_top"]
	sky_material.sky_horizon_color = palette["sky_horizon"]
	sky_material.ground_bottom_color = (
		(palette["sky_horizon"] as Color).darkened(0.2)
	)
	sky_material.ground_horizon_color = palette["sky_horizon"]
	var sky := Sky.new()
	sky.sky_material = sky_material

	var environment_resource := Environment.new()
	environment_resource.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment_resource.ambient_light_color = Color(0.74, 0.76, 0.78)
	environment_resource.ambient_light_energy = 1.2
	if presentation.has("ambient_color"):
		environment_resource.ambient_light_color = Color.html(
			str(presentation["ambient_color"])
		)
	if (
		presentation.has("ambient_energy")
		and _is_number(presentation["ambient_energy"])
	):
		environment_resource.ambient_light_energy = clampf(
			float(presentation["ambient_energy"]), 0.0, 2.0
		)
	environment_resource.background_mode = Environment.BG_SKY
	environment_resource.sky = sky
	var world_environment := WorldEnvironment.new()
	world_environment.name = "environment"
	world_environment.environment = environment_resource
	root.add_child(world_environment)

	parent.add_child(root)
	return {
		"ok": true,
		"root": root,
		"counts": {
			"bodies": body_count,
			"visuals": visual_count,
			"navmesh": navmesh_count,
		},
	}


static func _sky_palette(ground_material: String) -> Dictionary:
	# Derived, deterministic presentation only — no contract changes.
	match ground_material:
		"grass":
			# Warm day: soft blue sky, warm horizon.
			return {
				"sky_top": Color(0.55, 0.72, 0.92),
				"sky_horizon": Color(0.96, 0.86, 0.70),
				"sun": Color(1.0, 0.96, 0.88),
			}
		"snow":
			# Cold pale winter sky.
			return {
				"sky_top": Color(0.78, 0.84, 0.92),
				"sky_horizon": Color(0.88, 0.90, 0.94),
				"sun": Color(0.92, 0.94, 1.0),
			}
		_:
			# Dirt / rock / default → amber dusk.
			return {
				"sky_top": Color(0.42, 0.48, 0.72),
				"sky_horizon": Color(0.92, 0.62, 0.38),
				"sun": Color(1.0, 0.78, 0.55),
			}


static func _add_backdrop_mounds(
	root: Node3D,
	ground_origin: Vector3,
	ground_size: Vector2,
	ground_color: Color,
) -> void:
	# Ring of flattened spheres outside the playable ground. Deterministic from
	# a hash of the ground plane size; visual-only (no colliders / navmesh).
	var half_extent := maxf(ground_size.x, ground_size.y) * 0.5
	if half_extent <= 0.0:
		return
	var seed_state: int = hash("%s:%s" % [str(ground_size.x), str(ground_size.y)])
	if seed_state == 0:
		seed_state = 1
	var mound_material := StandardMaterial3D.new()
	mound_material.albedo_color = ground_color.darkened(0.25)
	for index: int in range(10):
		seed_state = _lcg_next(seed_state)
		var distance_factor := 1.6 + 0.35 * _lcg_unit(seed_state)
		seed_state = _lcg_next(seed_state)
		var radius_factor := 0.12 + 0.1 * _lcg_unit(seed_state)
		seed_state = _lcg_next(seed_state)
		var angle_jitter := (_lcg_unit(seed_state) - 0.5) * 0.35
		var angle := TAU * (float(index) / 10.0) + angle_jitter
		var distance := half_extent * distance_factor
		var radius := half_extent * radius_factor
		var mound := MeshInstance3D.new()
		mound.name = "mound_%d" % index
		var sphere := SphereMesh.new()
		sphere.radius = radius
		sphere.height = 2.0 * radius
		sphere.radial_segments = 10
		sphere.rings = 6
		mound.mesh = sphere
		mound.material_override = mound_material
		# Deliberate flatten — exempt from visual_y_scale compression path.
		# Sunk so only the crest rises above the horizon line.
		mound.scale = Vector3(1.0, 0.35, 1.0)
		mound.position = Vector3(
			ground_origin.x + cos(angle) * distance,
			-radius * 0.3,
			ground_origin.z + sin(angle) * distance,
		)
		root.add_child(mound)


static func _lcg_next(state: int) -> int:
	return int(posmod(state * 1664525 + 1013904223, 2147483647))


static func _lcg_unit(state: int) -> float:
	return float(posmod(state, 10000)) / 10000.0


static func _resolve_color(material_name: String, materials: Dictionary) -> Color:
	var entry_value: Variant = materials.get(material_name, null)
	if typeof(entry_value) == TYPE_DICTIONARY:
		var entry: Dictionary = entry_value
		var color_value: Variant = entry.get("color", null)
		if typeof(color_value) == TYPE_STRING:
			return Color.html(str(color_value))
	return MATERIAL_COLORS.get(material_name, MATERIAL_COLORS["default"])


static func _build_node(node: Dictionary, materials: Dictionary = {}) -> Dictionary:
	var node_id := str(node.get("node_id", ""))
	if node_id.is_empty():
		return {"ok": false, "error": "invalid node: missing node_id"}

	var transform_result: Dictionary = _parse_transform(node.get("transform"))
	if not transform_result.get("ok", false):
		return {"ok": false, "error": "invalid node: %s" % node_id}

	var container: Node3D
	var collider_value: Variant = node.get("collider", null)
	if typeof(collider_value) == TYPE_DICTIONARY:
		var collider: Dictionary = collider_value
		var collider_result: Dictionary = _build_collider_shape(collider)
		if not collider_result.get("ok", false):
			return {"ok": false, "error": collider_result.get("error", "")}
		var body := StaticBody3D.new()
		var collision := CollisionShape3D.new()
		collision.name = "collision"
		var collider_shape: Shape3D = collider_result["shape"]
		collision.shape = collider_shape
		body.add_child(collision)
		container = body
	else:
		container = Node3D.new()

	container.name = node_id
	var node_transform: Transform3D = transform_result["transform"]
	container.transform = node_transform
	container.set_meta("semantic_id", str(node.get("semantic_id", node_id)))
	if bool(node.get("navmesh_contributor", false)):
		container.add_to_group(NAVMESH_GROUP, true)

	var has_visual := false
	var visual_value: Variant = node.get("visual", null)
	if typeof(visual_value) == TYPE_DICTIONARY:
		var visual: Dictionary = visual_value
		var visual_result: Dictionary = _build_visual(visual, materials)
		if not visual_result.get("ok", false):
			container.free()
			return {"ok": false, "error": visual_result.get("error", "")}
		var mesh_instance: MeshInstance3D = visual_result["mesh_instance"]
		var y_scale := visual_y_scale()
		var visual_shape := str(visual.get("shape", ""))
		if (
			visual_shape != "plane"
			and visual_shape != "ribbon"
			and y_scale < 1.0
		):
			# Compress world-height about y=0: scaled center s*oy needs a
			# local shift of -(1-s)*oy since the mesh is a child of the node.
			# Planes and ribbons are already flat — skip compression.
			mesh_instance.scale = Vector3(1.0, y_scale, 1.0)
			mesh_instance.position.y = (
				-(1.0 - y_scale) * node_transform.origin.y
			)
		container.add_child(mesh_instance)
		has_visual = true

	return {"ok": true, "node": container, "has_visual": has_visual}


static func _build_collider_shape(collider: Dictionary) -> Dictionary:
	var shape_name := str(collider.get("shape", ""))
	var dimensions_value: Variant = collider.get("dimensions", null)
	match shape_name:
		"box":
			if typeof(dimensions_value) != TYPE_DICTIONARY:
				return {"ok": false, "error": "invalid node: box dimensions"}
			var dimensions: Dictionary = dimensions_value
			if (
				not dimensions.has("x")
				or not dimensions.has("y")
				or not dimensions.has("z")
				or not _is_number(dimensions["x"])
				or not _is_number(dimensions["y"])
				or not _is_number(dimensions["z"])
			):
				return {"ok": false, "error": "invalid node: box dimensions"}
			var box_shape := BoxShape3D.new()
			box_shape.size = Vector3(
				float(dimensions["x"]),
				float(dimensions["y"]),
				float(dimensions["z"]),
			)
			return {"ok": true, "shape": box_shape}
		"cylinder":
			if typeof(dimensions_value) != TYPE_DICTIONARY:
				return {"ok": false, "error": "invalid node: cylinder dimensions"}
			var dimensions: Dictionary = dimensions_value
			if (
				not dimensions.has("radius")
				or not dimensions.has("height")
				or not _is_number(dimensions["radius"])
				or not _is_number(dimensions["height"])
			):
				return {"ok": false, "error": "invalid node: cylinder dimensions"}
			var cylinder_shape := CylinderShape3D.new()
			cylinder_shape.radius = float(dimensions["radius"])
			cylinder_shape.height = float(dimensions["height"])
			return {"ok": true, "shape": cylinder_shape}
		_:
			return {
				"ok": false,
				"error": "unsupported collider shape: %s" % shape_name,
			}


static func _build_visual(visual: Dictionary, materials: Dictionary = {}) -> Dictionary:
	var shape_name := str(visual.get("shape", ""))
	var mesh: Mesh
	match shape_name:
		"box":
			var size_value: Variant = visual.get("size", null)
			if typeof(size_value) != TYPE_ARRAY:
				return {"ok": false, "error": "invalid node: visual fields"}
			var size: Array = size_value
			if (
				size.size() != 3
				or not _is_number(size[0])
				or not _is_number(size[1])
				or not _is_number(size[2])
			):
				return {"ok": false, "error": "invalid node: visual fields"}
			var box_mesh := BoxMesh.new()
			box_mesh.size = Vector3(float(size[0]), float(size[1]), float(size[2]))
			mesh = box_mesh
		"cylinder":
			if (
				not visual.has("radius")
				or not visual.has("height")
				or not _is_number(visual["radius"])
				or not _is_number(visual["height"])
			):
				return {"ok": false, "error": "invalid node: visual fields"}
			var bottom_radius := float(visual["radius"])
			var top_radius := bottom_radius
			if visual.has("top_radius") and visual["top_radius"] != null:
				if not _is_number(visual["top_radius"]):
					return {"ok": false, "error": "invalid node: visual fields"}
				top_radius = float(visual["top_radius"])
			var cylinder_mesh := CylinderMesh.new()
			cylinder_mesh.top_radius = top_radius
			cylinder_mesh.bottom_radius = bottom_radius
			cylinder_mesh.height = float(visual["height"])
			mesh = cylinder_mesh
		"plane":
			if (
				not visual.has("size_x")
				or not visual.has("size_z")
				or not _is_number(visual["size_x"])
				or not _is_number(visual["size_z"])
			):
				return {"ok": false, "error": "invalid node: visual fields"}
			var plane_mesh := PlaneMesh.new()
			plane_mesh.size = Vector2(float(visual["size_x"]), float(visual["size_z"]))
			mesh = plane_mesh
		"sphere":
			if not visual.has("radius") or not _is_number(visual["radius"]):
				return {"ok": false, "error": "invalid node: visual fields"}
			var sphere_radius := float(visual["radius"])
			var sphere_mesh := SphereMesh.new()
			sphere_mesh.radius = sphere_radius
			sphere_mesh.height = 2.0 * sphere_radius
			sphere_mesh.radial_segments = 10
			sphere_mesh.rings = 6
			mesh = sphere_mesh
		"ribbon":
			var ribbon_result: Dictionary = _build_ribbon_mesh(visual)
			if not ribbon_result.get("ok", false):
				return ribbon_result
			mesh = ribbon_result["mesh"]
		_:
			return {
				"ok": false,
				"error": "unknown visual shape: %s" % shape_name,
			}

	var material := StandardMaterial3D.new()
	var material_name := str(visual.get("material", "default"))
	material.albedo_color = _resolve_color(material_name, materials)
	# Bounded custom-material extras: emission, roughness, metallic only.
	var entry_value: Variant = materials.get(material_name, null)
	if typeof(entry_value) == TYPE_DICTIONARY:
		var entry: Dictionary = entry_value
		if typeof(entry.get("emission_color", null)) == TYPE_STRING:
			material.emission_enabled = true
			material.emission = Color.html(str(entry["emission_color"]))
			if _is_number(entry.get("emission_strength", null)):
				material.emission_energy_multiplier = clampf(
					float(entry["emission_strength"]), 0.0, 4.0
				)
		if _is_number(entry.get("roughness", null)):
			material.roughness = clampf(float(entry["roughness"]), 0.0, 1.0)
		if _is_number(entry.get("metallic", null)):
			material.metallic = clampf(float(entry["metallic"]), 0.0, 1.0)
	var mesh_instance := MeshInstance3D.new()
	mesh_instance.name = "visual"
	mesh_instance.mesh = mesh
	mesh_instance.material_override = material
	return {"ok": true, "mesh_instance": mesh_instance}


static func _build_ribbon_mesh(visual: Dictionary) -> Dictionary:
	var points_value: Variant = visual.get("points", null)
	if typeof(points_value) != TYPE_ARRAY:
		return {"ok": false, "error": "invalid node: visual fields"}
	var points: Array = points_value
	if points.size() < 2:
		return {"ok": false, "error": "invalid node: visual fields"}
	if not visual.has("width") or not _is_number(visual["width"]):
		return {"ok": false, "error": "invalid node: visual fields"}
	var width := float(visual["width"])
	if width <= 0.0:
		return {"ok": false, "error": "invalid node: visual fields"}

	var control: Array[Vector3] = []
	for point_value: Variant in points:
		if typeof(point_value) != TYPE_ARRAY:
			return {"ok": false, "error": "invalid node: visual fields"}
		var point: Array = point_value
		if (
			point.size() != 2
			or not _is_number(point[0])
			or not _is_number(point[1])
		):
			return {"ok": false, "error": "invalid node: visual fields"}
		control.append(Vector3(float(point[0]), 0.0, float(point[1])))

	var samples: Array[Vector3] = _catmull_rom_samples(control, 8)
	if samples.size() < 2:
		return {"ok": false, "error": "invalid node: visual fields"}

	var half := width * 0.5
	var vertices := PackedVector3Array()
	var normals := PackedVector3Array()
	var indices := PackedInt32Array()
	for i: int in range(samples.size()):
		var tangent := Vector3.ZERO
		if i == 0:
			tangent = samples[1] - samples[0]
		elif i == samples.size() - 1:
			tangent = samples[i] - samples[i - 1]
		else:
			tangent = samples[i + 1] - samples[i - 1]
		if tangent.length_squared() < 0.0000001:
			tangent = Vector3(1.0, 0.0, 0.0)
		else:
			tangent = tangent.normalized()
		var side := Vector3(-tangent.z, 0.0, tangent.x)
		if side.length_squared() < 0.0000001:
			side = Vector3(1.0, 0.0, 0.0)
		else:
			side = side.normalized()
		var center: Vector3 = samples[i]
		vertices.append(center - side * half)
		vertices.append(center + side * half)
		normals.append(Vector3.UP)
		normals.append(Vector3.UP)
		if i > 0:
			# Godot front faces wind CLOCKWISE; these triangles must read
			# clockwise from +Y or the ribbon is culled from above.
			var base := (i - 1) * 2
			indices.append(base)
			indices.append(base + 2)
			indices.append(base + 1)
			indices.append(base + 1)
			indices.append(base + 2)
			indices.append(base + 3)

	var arrays: Array = []
	arrays.resize(Mesh.ARRAY_MAX)
	arrays[Mesh.ARRAY_VERTEX] = vertices
	arrays[Mesh.ARRAY_NORMAL] = normals
	arrays[Mesh.ARRAY_INDEX] = indices
	var ribbon_mesh := ArrayMesh.new()
	ribbon_mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)
	return {"ok": true, "mesh": ribbon_mesh}


static func _catmull_rom_samples(
	control: Array[Vector3],
	subdivisions: int,
) -> Array[Vector3]:
	var samples: Array[Vector3] = []
	if control.size() < 2:
		return samples
	samples.append(control[0])
	for segment: int in range(control.size() - 1):
		var p0: Vector3 = control[maxi(segment - 1, 0)]
		var p1: Vector3 = control[segment]
		var p2: Vector3 = control[segment + 1]
		var p3: Vector3 = control[mini(segment + 2, control.size() - 1)]
		for step: int in range(1, subdivisions + 1):
			var t := float(step) / float(subdivisions)
			samples.append(_catmull_rom_point(p0, p1, p2, p3, t))
	return samples


static func _catmull_rom_point(
	p0: Vector3,
	p1: Vector3,
	p2: Vector3,
	p3: Vector3,
	t: float,
) -> Vector3:
	var t2 := t * t
	var t3 := t2 * t
	return 0.5 * (
		(2.0 * p1)
		+ (-p0 + p2) * t
		+ (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
		+ (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3
	)


static func _parse_transform(value: Variant) -> Dictionary:
	if typeof(value) != TYPE_DICTIONARY:
		return {"ok": false}
	var transform_value: Dictionary = value
	var origin_value: Variant = _parse_vec3(transform_value.get("origin"))
	var basis_x_value: Variant = _parse_vec3(transform_value.get("basis_x"))
	var basis_y_value: Variant = _parse_vec3(transform_value.get("basis_y"))
	var basis_z_value: Variant = _parse_vec3(transform_value.get("basis_z"))
	if (
		origin_value == null
		or basis_x_value == null
		or basis_y_value == null
		or basis_z_value == null
	):
		return {"ok": false}

	var origin: Vector3 = origin_value
	var basis_x: Vector3 = basis_x_value
	var basis_y: Vector3 = basis_y_value
	var basis_z: Vector3 = basis_z_value
	return {
		"ok": true,
		"transform": Transform3D(Basis(basis_x, basis_y, basis_z), origin),
	}


static func _parse_vec3(value: Variant) -> Variant:
	if typeof(value) != TYPE_DICTIONARY:
		return null
	var vector: Dictionary = value
	if (
		not vector.has("x")
		or not vector.has("y")
		or not vector.has("z")
		or not _is_number(vector["x"])
		or not _is_number(vector["y"])
		or not _is_number(vector["z"])
	):
		return null
	return Vector3(float(vector["x"]), float(vector["y"]), float(vector["z"]))


static func _is_number(value: Variant) -> bool:
	return typeof(value) == TYPE_INT or typeof(value) == TYPE_FLOAT
