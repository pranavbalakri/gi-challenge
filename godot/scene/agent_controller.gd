extends CharacterBody3D

const GRAVITY := 9.8
const PHYSICS_DELTA := 1.0 / 60.0
const AGENT_COLOR := Color(0.85, 0.2, 0.55)

var _agent: NavigationAgent3D = null
var _map := RID()


func setup(map: RID) -> void:
	_map = map
	var collision := CollisionShape3D.new()
	collision.name = "collision"
	var capsule := CapsuleShape3D.new()
	capsule.radius = 0.4
	capsule.height = 1.6
	collision.shape = capsule
	add_child(collision)
	var visual := MeshInstance3D.new()
	visual.name = "visual"
	var capsule_mesh := CapsuleMesh.new()
	capsule_mesh.radius = 0.4
	capsule_mesh.height = 1.6
	visual.mesh = capsule_mesh
	var material := StandardMaterial3D.new()
	material.albedo_color = AGENT_COLOR
	visual.material_override = material
	add_child(visual)
	_agent = NavigationAgent3D.new()
	_agent.name = "agent"
	_agent.path_desired_distance = 0.6
	_agent.target_desired_distance = 0.6
	add_child(_agent)
	_agent.set_navigation_map(_map)


func run_episode(
	target: Vector3,
	success_radius: float,
	max_ticks: int,
	stuck_timeout_ticks: int,
	speed: float,
) -> Dictionary:
	var planned := _route_length(global_position, target)
	var ticks := 0
	var travelled := 0.0
	var collisions := 0
	var stuck_ticks := 0
	var terminal := "timeout"
	var last := global_position
	_agent.target_position = target
	while ticks < max_ticks:
		await get_tree().physics_frame
		ticks += 1
		var planar_target := Vector3(target.x, global_position.y, target.z)
		if global_position.distance_to(planar_target) <= success_radius:
			terminal = "arrived"
			break
		var next := _agent.get_next_path_position()
		var direction := next - global_position
		direction.y = 0.0
		var new_velocity := Vector3.ZERO
		if direction.length() > 0.01:
			new_velocity = direction.normalized() * speed
		new_velocity.y = velocity.y - GRAVITY * PHYSICS_DELTA
		if is_on_floor():
			new_velocity.y = maxf(new_velocity.y, 0.0)
		velocity = new_velocity
		move_and_slide()
		if is_on_wall():
			collisions += 1
		var moved := global_position.distance_to(last)
		travelled += moved
		last = global_position
		if moved < 0.005:
			stuck_ticks += 1
			if stuck_ticks >= stuck_timeout_ticks:
				terminal = "stuck"
				break
		else:
			stuck_ticks = 0
	return {
		"terminal_reason": terminal,
		"ticks_used": ticks,
		"path_length_m": travelled,
		"final_geodesic_distance_m": _route_length(global_position, target),
		"collisions": collisions,
		"stuck_recoveries": 0,
		"planned_path_length_m": planned,
	}


func _route_length(from: Vector3, to: Vector3) -> float:
	var path := NavigationServer3D.map_get_path(_map, from, to, true)
	var total := 0.0
	for index: int in range(1, path.size()):
		total += path[index - 1].distance_to(path[index])
	return total
