extends CharacterBody3D

const GRAVITY := 9.8
const PHYSICS_DELTA := 1.0 / 60.0
const AGENT_COLOR := Color(0.85, 0.2, 0.55)

const WANDER_ARRIVE_DISTANCE := 1.0
const WANDER_STUCK_FRAMES := 45  # ~0.75 s without progress -> new target
const WANDER_LEG_TIMEOUT_FRAMES := 600  # ~10 s per leg hard cap
const WANDER_PROGRESS_EPSILON := 0.012

var _agent: NavigationAgent3D = null
var _map := RID()
var _visual: Node3D = null
var _wander := false
var _wander_speed := 3.0
var _wander_target := Vector3.ZERO
var _wander_last := Vector3.ZERO
var _wander_stuck_frames := 0
var _wander_leg_frames := 0
var _wander_retargets := 0


func setup(map: RID) -> void:
	_map = map
	var collision := CollisionShape3D.new()
	collision.name = "collision"
	var capsule := CapsuleShape3D.new()
	capsule.radius = 0.4
	capsule.height = 1.6
	collision.shape = capsule
	add_child(collision)
	# Flat map-marker visual (disc + heading arrow) instead of a capsule:
	# reads as a top-down token while the invisible capsule collider stays.
	var visual := Node3D.new()
	visual.name = "visual"
	var material := StandardMaterial3D.new()
	material.albedo_color = AGENT_COLOR
	var disc := MeshInstance3D.new()
	disc.name = "disc"
	var disc_mesh := CylinderMesh.new()
	disc_mesh.top_radius = 0.45
	disc_mesh.bottom_radius = 0.45
	disc_mesh.height = 0.22
	disc.mesh = disc_mesh
	disc.material_override = material
	disc.position = Vector3(0.0, -0.55, 0.0)
	visual.add_child(disc)
	var arrow := MeshInstance3D.new()
	arrow.name = "arrow"
	var arrow_mesh := BoxMesh.new()
	arrow_mesh.size = Vector3(0.22, 0.1, 0.5)
	arrow.mesh = arrow_mesh
	arrow.material_override = material
	arrow.position = Vector3(0.0, -0.49, -0.55)
	visual.add_child(arrow)
	add_child(visual)
	_visual = visual
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
	_wander = false
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
		var planar := Vector3(velocity.x, 0.0, velocity.z)
		if _visual != null and planar.length() > 0.05:
			_visual.rotation.y = atan2(-planar.x, -planar.z)
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


func start_wander(speed: float = 3.0) -> void:
	## Continuous plausible traversal: walk to random navmesh points forever.
	## The navmesh already excludes water and carves around blockers, so the
	## wander stays on legal ground by construction.
	_wander_speed = speed
	_wander = true
	_pick_wander_target()


func stop_wander() -> void:
	_wander = false


func wander_retargets() -> int:
	return _wander_retargets


func _pick_wander_target() -> void:
	# Random navmesh points can sit across water or in pinched slivers the
	# agent cannot actually reach; NavigationAgent3D then clamps the path to
	# the nearest boundary and the agent grinds against it forever. Prefer
	# candidates whose computed path really terminates near the candidate.
	for _attempt: int in range(6):
		var point := NavigationServer3D.map_get_random_point(_map, 1, true)
		if global_position.distance_to(point) < 2.0:
			continue
		var path := NavigationServer3D.map_get_path(
			_map, global_position, point, true
		)
		if (
			path.size() >= 2
			and path[path.size() - 1].distance_to(point) <= 1.0
		):
			_set_wander_target(point)
			return
	# Fallback: take anything; the watchdog below recovers from bad picks.
	_set_wander_target(NavigationServer3D.map_get_random_point(_map, 1, true))


func _set_wander_target(point: Vector3) -> void:
	_wander_target = point
	_agent.target_position = point
	_wander_last = global_position
	_wander_stuck_frames = 0
	_wander_leg_frames = 0
	_wander_retargets += 1


func _physics_process(_delta: float) -> void:
	if not _wander:
		return
	_wander_leg_frames += 1
	var planar_target := Vector3(_wander_target.x, global_position.y, _wander_target.z)
	if (
		global_position.distance_to(planar_target) <= WANDER_ARRIVE_DISTANCE
		or _agent.is_navigation_finished()
		or _wander_leg_frames >= WANDER_LEG_TIMEOUT_FRAMES
	):
		# Arrived, or the path ended short of an unreachable target, or the
		# leg ran too long: either way, walk somewhere new.
		_pick_wander_target()
		return
	var next := _agent.get_next_path_position()
	var direction := next - global_position
	direction.y = 0.0
	var desired := Vector3.ZERO
	if direction.length() > 0.01:
		desired = direction.normalized() * _wander_speed
	# Smooth planar velocity: raw per-frame retargeting oscillates against
	# navmesh boundaries and reads as stutter.
	var planar_velocity := Vector3(velocity.x, 0.0, velocity.z)
	planar_velocity = planar_velocity.lerp(desired, 0.25)
	var vertical := velocity.y - GRAVITY * PHYSICS_DELTA
	if is_on_floor():
		vertical = maxf(vertical, 0.0)
	velocity = Vector3(planar_velocity.x, vertical, planar_velocity.z)
	move_and_slide()
	var moved := global_position.distance_to(_wander_last)
	_wander_last = global_position
	if moved < WANDER_PROGRESS_EPSILON:
		_wander_stuck_frames += 1
		if _wander_stuck_frames >= WANDER_STUCK_FRAMES:
			_pick_wander_target()
			return
	else:
		_wander_stuck_frames = 0
	var planar := Vector3(velocity.x, 0.0, velocity.z)
	if _visual != null and planar.length() > 0.05:
		var heading := atan2(-planar.x, -planar.z)
		_visual.rotation.y = lerp_angle(_visual.rotation.y, heading, 0.2)


func _route_length(from: Vector3, to: Vector3) -> float:
	var path := NavigationServer3D.map_get_path(_map, from, to, true)
	var total := 0.0
	for index: int in range(1, path.size()):
		total += path[index - 1].distance_to(path[index])
	return total
