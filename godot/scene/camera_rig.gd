extends Node3D

const ISO_PITCH_DEGREES := 35.264
const ISO_YAW_DEGREES := 45.0
const FOLLOW_DISTANCE := 30.0
const TOPDOWN_HEIGHT := 30.0

var _viewport: SubViewport = null
var _camera: Camera3D = null


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


func frame_isometric(target: Vector3) -> void:
	_camera.rotation_degrees = Vector3(-ISO_PITCH_DEGREES, ISO_YAW_DEGREES, 0.0)
	_camera.position = target + _camera.transform.basis.z * FOLLOW_DISTANCE


func frame_topdown(target: Vector3) -> void:
	_camera.rotation_degrees = Vector3(-90.0, 0.0, 0.0)
	_camera.position = Vector3(target.x, TOPDOWN_HEIGHT, target.z)


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
