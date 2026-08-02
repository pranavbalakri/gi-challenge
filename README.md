# gi-challenge

Hi! This contains my submission to the challenge. 

Overall, it constructs an isometrically-displayed 2D environment (sort of like Hades) that an agent can move through, running on Godot. There is an SDK containing objects/materials and the harness allows the ability to generate custom assets. I was inspired by [Articraft](https://arxiv.org/html/2605.15187v1#S3)'s SDK. The harness returns structured runtime feedback, and iteratively repairs the program until the environment is valid and navigable.

Worlds render as organic low-poly scenes — curved spline lanes, sphere-canopy oaks and conical pines, boulders, and rolling backdrop hills under a material-derived sky. The model places things deliberately (`prop()`, opt-in scatter variation) and can audit screenshots of its own world mid-loop (`audit_render`) to improve the composition before sealing.

#### Running It

Running the harness can be done as described below, in Claude Code. I did it in the terminal.

**Agent-driven mode (the Claude Code / Codex way):** the coding agent itself is the authoring model — no OpenAI key needed. Open your agent in the repo and tell it:

> Author an environment with the envmaker harness: run `uv run envmaker author init "<your prompt>"`, follow the printed instructions, write `runs/<id>/environment.py` yourself, run `uv run envmaker author step <run_dir>` after each edit, open and look at the render PNGs it prints, and iterate until it prints ACCEPTED. Then show me with `uv run envmaker author open <run_dir>`.

The harness validates (nine hard stages), simulates traversal, renders, and seals `environment-definition.json`; the agent does all the authoring and visual judgment with its own file tools and vision. (`envmaker run` remains the autonomous API-keyed mode; `demo` and `check` stay keyless.)

**Requirements:** Python 3.12, [uv](https://docs.astral.sh/uv/), and a Godot 4.7.1 binary — either placed at `tools/godot/Godot.app` (kept local; not committed to git) or pointed to via `GODOT_BIN=<path>`. Developed and verified on macOS arm64; Linux is best-effort via `GODOT_BIN` (headless boxes need a virtual display such as `xvfb-run` for the windowed render capture); Windows is untested.

```bash
uv sync
uv run python scripts/verify_toolchain.py   # verifies the Python + Godot pins
```

**The four commands:**

```bash
# 1. Keyless demo: runs the checked-in fixture (examples/demo/environment.py)
#    through all nine hard validators, navigates to the landmark, captures renders.
uv run envmaker demo --headless
# -> nine "PASS <stage>" lines, render paths, and run_dir: runs/<id>/

# 2. Same, but with a visible Godot window that plays the traversal.
uv run envmaker demo --view

# 3. Live authoring (needs OPENAI_API_KEY in .env): generates environment.py from
#    the prompt, streams compile/repair events, and preserves every attempt.
uv run envmaker run "a frozen village with a walled square and a watchtower" --seed 7
# -> exit 0 for accepted | rejected_after_budget; nonzero only for system failures

# 4. Keyless contract suite: pytest (non-recursive), the Godot in-engine harness,
#    and the headless demo, aggregated into three PASS/FAIL lines.
uv run envmaker check
```

**What a run leaves behind** (`runs/<id>/`): `runlog.jsonl` (redacted event trace), `revisions/rev-N.py` (every source revision), `runtime/artifacts/*.png` (content-addressed isometric + top-down renders), and — only on acceptance — `environment-definition.json`, the sealed, canonically fingerprinted definition.

**Tests:** `uv run pytest -q` runs the full suite (the live-Godot tests skip themselves in sandboxed shells). The hard gate for the repair loop is the deterministic scripted two-repair fixture in `tests/agent/test_repair_loop.py` — spawn-in-blocker failure, patch, clear-ground connectivity failure, second patch, traversal, sealed definition — no API key required.

**Evaluation:** six prompts × seed 7 × {one-shot, repair-loop} against `gpt-5.4-mini` (16-turn budget; the loop's workflow includes a mandatory screenshot self-audit). One-shot accepted 3/6, the loop 4/6 — and the loop's wins include the hard prompts one-shot never solves (`frozen_village`, `desert_canyon`), with every repair visible in the traces. Full tables, failed examples, and limitations: [`evals/mvp-report.md`](evals/mvp-report.md).
