# EnvMaker — agent guide

You are running inside EnvMaker: an agent harness that turns text prompts into
playable, validated Godot environments. **You are the authoring model.** The
harness compiles, validates (nine hard stages), simulates an agent traversing
the world, renders it, and seals accepted environments — your job is to write
and repair the environment program it validates.

## First-time setup (run these before anything else)

```bash
uv sync                                   # Python 3.12 env (installs everything)
uv run python scripts/get_godot.py       # downloads Godot 4.7.1 if tools/godot/ is absent
uv run python scripts/verify_toolchain.py # confirms the pins
```

Notes: `uv` install (if missing): `curl -LsSf https://astral.sh/uv/install.sh | sh`.
If you already have a Godot 4.7.1 binary, `export GODOT_BIN=<path>` instead of
downloading. Developed and verified on macOS arm64; Linux works via the same
script (headless boxes need a virtual display, e.g. `xvfb-run`, for renders).

## Authoring an environment (the main flow — no API key needed)

When the human asks you to build an environment:

```bash
uv run envmaker author init "<their prompt>" --seed 7
```

Read everything it prints — it contains the full SDK contract. Then:

1. Write the complete program into the `environment.py` it points you to.
2. `uv run envmaker author step <run_dir>` — typed failure signals tell you
   exactly what to fix, with measurements and guidance.
3. Open and LOOK at the render PNGs it prints. Check the world against the
   human's request and the composition.
4. Iterate until it prints `ACCEPTED`.
5. Show the human: `uv run envmaker author open <run_dir>` (opens a maximized
   window with the agent wandering the world until they close it).

Rules: author only through the SDK in `environment.py`. Never modify the
harness, validators, or tests to make a world pass — hard validity is the
product. If a signal seems wrong, tell the human instead.

## Other commands

```bash
uv run envmaker demo --view      # keyless fixture demo in a visible window
uv run envmaker check            # keyless contract suite (pytest + Godot + demo)
uv run envmaker run "<prompt>"   # autonomous API mode (needs OPENAI_API_KEY in .env)
uv run pytest -q                 # full test suite (live Godot tests skip if sandboxed)
```

More detail: `README.md` (usage), `documents/architecture.md` (design),
`evals/mvp-report.md` (evaluation results).
