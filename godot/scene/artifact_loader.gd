extends RefCounted


static func validate_relpath(path: String) -> String:
	if path.is_empty():
		return "artifact path must not be empty"
	if path.length() > 512:
		return "artifact path must be at most 512 characters"
	if path.contains(String.chr(0)):
		return "artifact path must not contain NUL"
	if path.contains("\\"):
		return "artifact path must use forward slashes only"
	if path.contains(":"):
		return "artifact path must not contain ':'"
	if path.begins_with("/"):
		return "artifact path must not be absolute"

	for segment: String in path.split("/", true):
		if segment.is_empty():
			return "artifact path segments must not be empty"
		if segment == "." or segment == "..":
			return "artifact path segments must not be '.' or '..'"
	return ""


static func verify_ref(run_root: String, ref: Dictionary) -> String:
	var path_value: Variant = ref.get("path", "")
	if typeof(path_value) != TYPE_STRING:
		return "artifact path must be a string"
	var relative_path: String = path_value
	var path_error := validate_relpath(relative_path)
	if not path_error.is_empty():
		return path_error

	var simplified_root := run_root.simplify_path()
	var full_path := simplified_root.path_join(relative_path).simplify_path()
	if not full_path.begins_with(simplified_root + "/"):
		return "path escapes run root"

	var current_dir := simplified_root
	for segment: String in relative_path.split("/", true):
		var parent_dir := DirAccess.open(current_dir)
		if parent_dir != null and parent_dir.is_link(segment):
			return "symlink artifact rejected"
		current_dir = current_dir.path_join(segment)
	if not FileAccess.file_exists(full_path):
		return "missing artifact file"

	var artifact_file := FileAccess.open(full_path, FileAccess.READ)
	if artifact_file == null:
		return "missing artifact file"
	if artifact_file.get_length() != int(ref.get("byte_count", -1)):
		return "size mismatch"
	if FileAccess.get_sha256(full_path) != str(ref.get("sha256", "")):
		return "digest mismatch"
	return ""


static func verify_manifest(run_root: String, manifest: Dictionary) -> String:
	var entries_value: Variant = manifest.get("entries", [])
	if typeof(entries_value) != TYPE_ARRAY:
		return "entries: manifest entries must be an array"
	for entry_value: Variant in entries_value:
		if typeof(entry_value) != TYPE_DICTIONARY:
			return ": artifact reference must be a dictionary"
		var entry: Dictionary = entry_value
		var error := verify_ref(run_root, entry)
		if not error.is_empty():
			return "%s: %s" % [str(entry.get("path", "")), error]
	return ""
