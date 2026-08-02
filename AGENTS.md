# EnvMaker — agent guide

This file mirrors `CLAUDE.md` for agents that read `AGENTS.md` (e.g. Codex).
Read `CLAUDE.md` for the full guide; the short version:

1. Setup: `uv sync`, then `uv run python scripts/get_godot.py` (downloads the
   pinned Godot 4.7.1 if `tools/godot/` is absent; or set `GODOT_BIN=<path>`).
2. You are the authoring model. To build an environment for the human:
   `uv run envmaker author init "<prompt>"`, write the `environment.py` it
   points you to, then `uv run envmaker author step <run_dir>` after each edit.
   Typed signals tell you what to fix; open and look at the render PNGs it
   prints; iterate until `ACCEPTED`; show the human with
   `uv run envmaker author open <run_dir>`.
3. Author only through the SDK in `environment.py`. Never modify the harness,
   validators, or tests to make a world pass.

Keyless verification: `uv run envmaker check` and `uv run envmaker demo --view`.
