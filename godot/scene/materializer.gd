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


static func materialize(candidate: Dictionary, parent: Node) -> Dictionary:
	var scene_value: Variant = candidate.get("scene")
	if typeof(scene_value) != TYPE_DICTIONARY:
		return {"ok": false, "error": "invalid candidate"}
	var scene: Dictionary = scene_value
	var nodes_value: Variant = scene.get("nodes")
	if typeof(nodes_value) != TYPE_ARRAY:
		return {"ok": false, "error": "invalid candidate"}
	var nodes: Array = nodes_value

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
		var node_result: Dictionary = _build_node(node)
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

	var sun := DirectionalLight3D.new()
	sun.name = "sun"
	sun.rotation_degrees = Vector3(-55.0, -35.0, 0.0)
	root.add_child(sun)

	var environment_resource := Environment.new()
	environment_resource.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment_resource.ambient_light_color = Color(0.55, 0.58, 0.62)
	environment_resource.ambient_light_energy = 1.0
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


static func _build_node(node: Dictionary) -> Dictionary:
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
		var visual_result: Dictionary = _build_visual(visual)
		if not visual_result.get("ok", false):
			container.free()
			return {"ok": false, "error": visual_result.get("error", "")}
		var mesh_instance: MeshInstance3D = visual_result["mesh_instance"]
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


static func _build_visual(visual: Dictionary) -> Dictionary:
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
			var cylinder_mesh := CylinderMesh.new()
			cylinder_mesh.top_radius = float(visual["radius"])
			cylinder_mesh.bottom_radius = float(visual["radius"])
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
		_:
			return {
				"ok": false,
				"error": "unknown visual shape: %s" % shape_name,
			}

	var material := StandardMaterial3D.new()
	var material_name := str(visual.get("material", "default"))
	var albedo_color: Color = MATERIAL_COLORS.get(
		material_name,
		MATERIAL_COLORS["default"],
	)
	material.albedo_color = albedo_color
	var mesh_instance := MeshInstance3D.new()
	mesh_instance.name = "visual"
	mesh_instance.mesh = mesh
	mesh_instance.material_override = material
	return {"ok": true, "mesh_instance": mesh_instance}


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
