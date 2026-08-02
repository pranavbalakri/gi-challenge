extends Node3D

const DEFAULT_ISO_PITCH_DEGREES := 65.0
const ISO_YAW_DEGREES := 45.0
const FOLLOW_DISTANCE := 30.0
const TOPDOWN_HEIGHT := 30.0


static func iso_pitch_degrees() -> float:
	# Presentation knob: steeper pitch reads flatter / more "2D isometric".
	# ENVMAKER_ISO_PITCH overrides (degrees, clamped 20..80).
	var raw := OS.get_environment("ENVMAKER_ISO_PITCH")
	if raw.is_empty() or not raw.is_valid_float():
		return DEFAULT_ISO_PITCH_DEGREES
	return clampf(raw.to_float(), 20.0, 80.0)

var _viewport: SubViewport = null
var _camera: Camera3D = null
var _presenter: TextureRect = null
var _follow_target: Node3D = null
var _follow_suspended := false


func setup(orthographic_size: float, viewport_size: Vector2i = Vector2i(1280, 720)) -> void:
	_viewport = SubViewport.new()
	_viewport.name = "viewport"
	_viewport.size = viewport_size
	_viewport.render_target_update_mode = SubViewport.UPDATE_ALWAYS
	_viewport.own_world_3d = false
	add_child(_viewport)
	_camera = Camera3D.new()
	_camera.name = "camera"
	_camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	_camera.size = orthographic_size
	_viewport.add_child(_camera)
	_camera.current = true
	_present_to_window()


func _present_to_window() -> void:
	# The world renders only into the offscreen SubViewport (that is what
	# capture() reads). With a real display server the OS window would
	# otherwise stay blank, so mirror the SubViewport onto a full-window
	# TextureRect. Headless runs skip this.
	if DisplayServer.get_name() == "headless":
		return
	_presenter = TextureRect.new()
	_presenter.name = "view_presenter"
	_presenter.texture = _viewport.get_texture()
	_presenter.stretch_mode = TextureRect.STRETCH_KEEP_ASPECT_CENTERED
	_presenter.expand_mode = TextureRect.EXPAND_IGNORE_SIZE
	_presenter.set_anchors_preset(Control.PRESET_FULL_RECT)
	get_tree().root.add_child.call_deferred(_presenter)


func _exit_tree() -> void:
	if _presenter != null and is_instance_valid(_presenter):
		_presenter.queue_free()
		_presenter = null


func frame_isometric(target: Vector3) -> void:
	_follow_suspended = false
	_camera.rotation_degrees = Vector3(-iso_pitch_degrees(), ISO_YAW_DEGREES, 0.0)
	_camera.position = target + _camera.transform.basis.z * FOLLOW_DISTANCE


func frame_topdown(target: Vector3) -> void:
	# Explicit top-down framing (capture) pauses follow so the shot is stable;
	# the next frame_isometric() resumes it.
	_follow_suspended = true
	_camera.rotation_degrees = Vector3(-90.0, 0.0, 0.0)
	_camera.position = Vector3(target.x, TOPDOWN_HEIGHT, target.z)


func set_follow_target(target: Node3D) -> void:
	_follow_target = target


func _process(_delta: float) -> void:
	if _follow_suspended or _camera == null:
		return
	if _follow_target == null or not is_instance_valid(_follow_target):
		return
	_camera.rotation_degrees = Vector3(-iso_pitch_degrees(), ISO_YAW_DEGREES, 0.0)
	_camera.position = (
		_follow_target.global_position
		+ _camera.transform.basis.z * FOLLOW_DISTANCE
	)


func capture(path: String) -> Dictionary:
	if RenderingServer.get_video_adapter_name().is_empty():
		return {"ok": false, "error": "rendering unavailable"}
	for i: int in range(2):
		await get_tree().process_frame
	RenderingServer.force_draw()
	var image: Image = _viewport.get_texture().get_image()
	if image == null:
		return {"ok": false, "error": "rendering unavailable"}
	if image.save_png(path) != OK:
		return {"ok": false, "error": "png save failed"}
	var byte_count := FileAccess.get_file_as_bytes(path).size()
	return {
		"ok": true,
		"path": path,
		"byte_count": byte_count,
		"sha256": FileAccess.get_sha256(path),
	}
