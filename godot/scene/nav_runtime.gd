extends Node3D

const Materializer = preload("res://scene/materializer.gd")

signal status_changed(status: String)

var _status := "unloaded"
var _region: NavigationRegion3D = null
var _navigation_mesh: NavigationMesh = null
var _map := RID()


func status() -> String:
	return _status


func setup(agent_radius: float) -> void:
	_navigation_mesh = NavigationMesh.new()
	_navigation_mesh.geometry_parsed_geometry_type = (
		NavigationMesh.PARSED_GEOMETRY_STATIC_COLLIDERS
	)
	_navigation_mesh.geometry_source_geometry_mode = (
		NavigationMesh.SOURCE_GEOMETRY_GROUPS_WITH_CHILDREN
	)
	_navigation_mesh.geometry_source_group_name = Materializer.NAVMESH_GROUP
	_navigation_mesh.agent_radius = agent_radius
	_map = NavigationServer3D.map_create()
	NavigationServer3D.map_set_active(_map, true)
	NavigationServer3D.map_set_use_async_iterations(_map, false)
	_region = NavigationRegion3D.new()
	_region.name = "region"
	_region.navigation_mesh = _navigation_mesh
	add_child(_region)
	_region.set_navigation_map(_map)


func bake(timeout_seconds: float, probe_from: Vector3, probe_to: Vector3) -> void:
	if _region == null or not is_inside_tree():
		_set_status("failed")
		return
	var deadline := Time.get_ticks_msec() + int(timeout_seconds * 1000.0)
	_set_status("parsing")
	var source := NavigationMeshSourceGeometryData3D.new()
	NavigationServer3D.parse_source_geometry_data(_navigation_mesh, source, self)
	if not source.has_data():
		_set_status("failed")
		return
	_set_status("baking")
	NavigationServer3D.bake_from_source_geometry_data(_navigation_mesh, source)
	if _navigation_mesh.get_polygon_count() == 0:
		_set_status("failed")
		return
	_region.navigation_mesh = _navigation_mesh
	while (
		NavigationServer3D.map_get_path(_map, probe_from, probe_to, true).size() < 2
		and Time.get_ticks_msec() < deadline
	):
		await get_tree().physics_frame
	if NavigationServer3D.map_get_path(_map, probe_from, probe_to, true).size() < 2:
		_set_status("failed")
		return
	_set_status("ready")


func map_rid() -> RID:
	return _map


func path_between(from: Vector3, to: Vector3) -> PackedVector3Array:
	if _status != "ready":
		return PackedVector3Array()
	return NavigationServer3D.map_get_path(_map, from, to, true)


func path_length(path: PackedVector3Array) -> float:
	var total := 0.0
	for index: int in range(1, path.size()):
		total += path[index - 1].distance_to(path[index])
	return total


func _set_status(status: String) -> void:
	_status = status
	status_changed.emit(status)


func _exit_tree() -> void:
	if _map.is_valid():
		NavigationServer3D.free_rid(_map)
		_map = RID()
